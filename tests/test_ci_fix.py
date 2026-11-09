import copy
import json
from pathlib import Path
from typing import Any

import pytest

from factory.config import display_path
from harness import NO_RUN_STAGES, AgentScript, Harness, put_graph, run_id_of
from test_ci_wait import (
    LOG,
    PASS,
    RED,
    RUNNING,
    STALE,
    UNIT,
    Answerer,
    Crash,
    ci_run,
    killed_runs_run_no_handler,  # noqa: F401
)
from test_describer import DESCRIPTION

FIXED = {
    "summary": "Stop reading the clock in the test",
    "failure": "test_run_lasts_0s saw 1s on a slow runner",
    "change": "the test no longer depends on the machine's speed",
    "handoff": "removed the dependency",
}
FIX = AgentScript(files={"fixed.txt": "fixed\n"}, output=FIXED, cost=0.25)
BREAKS_THE_GATE = AgentScript(files={"broken.txt": "oops\n"}, output={**FIXED, "handoff": "tried and broke the build"})
TEST_FILE = "tests/test_clock.py"
TEST = "def test_run_lasts_0s():\n    assert 1 == 1\n"


def run_graph_with_ci_fix(factory: Harness, stage: dict[str, Any], gates: dict[str, Any]) -> None:
    """Makes the project's Run graph the harness's one, with the Stage `ci_fix` and `gates` on the way back to `ci`."""
    graph = copy.deepcopy(NO_RUN_STAGES)
    graph["nodes"]["ci"]["next"]["red"] = "ci_fix"
    graph["nodes"] |= {"ci_fix": stage, **gates}
    put_graph(factory, "with-ci-fix", graph)


def with_ci_fix(factory: Harness, rounds: int = 2, **fields: object) -> Path:
    """Gives the Run graph a CI fixer whose Gate `ci_build` fails while `broken.txt` exists, and returns a Spec."""
    spec = ci_run(factory)
    run_graph_with_ci_fix(
        factory,
        {
            "kind": "ci_fix",
            "role": "ci-fixer",
            "rounds": {"max": rounds, "then": "ci_failed"},
            "next": {"done": "ci_build", "failed": "ci_fix"},
            **fields,
        },
        {
            "ci_build": {
                "kind": "command",
                "with": {"run": "test ! -f broken.txt"},
                "next": {"pass": "ci", "fail": "ci_fix"},
            }
        },
    )
    factory.executor.scripts["ci_fix"] = FIX
    factory.gh.log(91, LOG)
    return spec


def fixer(factory: Harness, run_id: str, type: str) -> list[dict[str, Any]]:
    """Returns the events of `type` for the Attempts of the CI fixer, which the Run's Tickets do not share."""
    return [e for e in factory.events(run_id, type) if e["ticket"] == "/ci_fix"]


def test_red_ci_starts_the_fixer_whose_commit_is_pushed_and_green_ci_on_the_new_head_ends_the_run_done(
    factory: Harness,
) -> None:
    spec = with_ci_fix(factory)
    factory.gh.checks([RUNNING], [RED], [PASS])
    working: list[list[tuple[str, str]]] = []
    waiting: list[list[tuple[str, str]]] = []
    statuses: list[str] = []

    def look(request: object) -> None:
        run_id = factory.run_ids()[-1]
        working.append(factory.slots())
        statuses.append(factory.cli("status", run_id).out)

    factory.executor.on_start = lambda request: look(request) if request.stage == "ci_fix" else None

    def on_sleep(seconds: float) -> None:
        run_id = factory.run_ids() and factory.run_ids()[-1]
        fixing = run_id and len(factory.events(run_id, "CiFixStarted")) != len(factory.events(run_id, "CiFixFinished"))
        if run_id and factory.events(run_id, "CiStarted") and not fixing:
            waiting.append(factory.slots())

    factory.clock.on_sleep = on_sleep

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [started] = factory.events(run_id, "CiFixStarted")
    [attempt] = fixer(factory, run_id, "AttemptStarted")
    base = factory.git("rev-parse", "main")
    first, second = (e["commit"] for e in factory.events(run_id, "CiStarted"))
    assert (started["round"], started["commit"], started["jobs"]) == (1, first, [{"name": "unit", "url": UNIT}])
    brief = attempt["brief"]
    assert (attempt["ticket"], attempt["node"], brief["stage"], brief["round"]) == ("/ci_fix", "ci_fix", "ci_fix", 1)
    ci_log, diff = brief["inputs"]
    assert ci_log["jobs"] == started["jobs"] and LOG in Path(ci_log["path"]).read_text()
    assert (diff["base"], diff["head"], brief["base"]) == (base, first, base)

    [pushed] = factory.events(run_id, "IntegrationBranchPushed")[1:]
    assert pushed["commit"] == second == factory.remote_branches()[f"factory/{run_id}"]
    assert factory.git("log", "-1", "--format=%s", second) == FIXED["summary"]
    assert f"Factory-Run: {run_id}" in factory.git("log", "-1", "--format=%b", second)
    assert factory.git("rev-parse", f"factory/{run_id}") == second
    assert factory.git("rev-parse", f"{second}^") == first
    red, finished = factory.events(run_id, "CiFinished")
    assert (red["outcome"], red["commit"], red["jobs"]) == ("failed", first, [{"name": "unit", "url": UNIT}])
    assert (finished["outcome"], finished["commit"]) == ("passed", second)
    [report] = factory.events(run_id, "CiFixFinished")
    assert (report["outcome"], report["pushed"]) == ("pushed", second)
    assert [e["outcome"] for e in factory.events(run_id, "CiFixGateFinished")] == ["pass", "pass"]
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]
    [pull] = factory.gh.pulls()
    assert pull["body"].endswith(
        f"CI fixes:\n\n- Round 1: {FIXED['failure']}. Fixed by: {FIXED['change']} ({second[:7]})\n"
    )
    assert working == [[(run_id, "/ci_fix")]]
    assert waiting and all(slots == [] for slots in waiting)
    assert "Run phase: ci_fix" in statuses[0].splitlines()
    assert any(
        line.startswith(f"ci_fix: the fixer works on the red CI of {first[:7]}, Round 1, Attempt 1")
        for line in statuses[0].splitlines()
    )
    assert not (factory.home / "workspaces" / f"cifix-{run_id}").exists()
    assert f"factory/cifix-{run_id}" not in factory.branches()
    [line] = [row for row in factory.cli("status", run_id, "--timeline").out.splitlines() if " ci_fix " in row]
    assert line.split()[2:8] == ["ci_fix", "pushed", "round", "1", "attempt", "1"] and line.endswith("$0.25")
    assert result.out.splitlines()[-2].endswith("$0.25, CI passed")
    [finished] = fixer(factory, run_id, "AttemptFinished")
    assert finished["outcome"] == "done"
    assert finished["usage"]["cost_usd"] == 0.25 and finished["duration"] >= 0


def test_failed_gate_after_the_fixer_starts_the_next_round_with_its_verdict_and_the_integration_branch_stays_put(
    factory: Harness,
) -> None:
    spec = with_ci_fix(factory)
    factory.executor.scripts["ci_fix"] = [BREAKS_THE_GATE, FIX]
    factory.gh.checks([RUNNING], [RED], [PASS])

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    first, second = factory.events(run_id, "CiFixFinished")
    assert (first["outcome"], first["cause"]) == ("failed", "gate")
    assert first["reason"].startswith("Gate ci_build failed")
    assert second["outcome"] == "pushed"
    briefs = [e["brief"] for e in fixer(factory, run_id, "AttemptStarted")]
    assert [b["round"] for b in briefs] == [1, 2]
    sources = [(i["source"], i.get("round")) for i in briefs[1]["inputs"]]
    assert sources == [("ci_log", None), ("diff", None), ("stage", 1), ("cause", None), ("gate", 1), ("gate", 1)]
    [verdict] = [
        i for i in briefs[1]["inputs"] if i["source"] == "gate" and i["path"].endswith("ci_build/probe-1/verdict.json")
    ]
    assert json.loads(Path(verdict["path"]).read_text())["outcome"] == "fail"
    assert [e["commit"] for e in factory.events(run_id, "CiStarted")][0] == briefs[1]["head"]
    [pull] = factory.gh.pulls()
    assert "- Round 1: " in pull["body"] and "Not pushed: Gate ci_build failed" in pull["body"]
    assert "- Round 2: " in pull["body"] and "Fixed by: " in pull["body"]
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]


def test_red_ci_after_the_last_round_opens_ci_failed_with_every_rounds_failed_jobs(factory: Harness) -> None:
    spec = with_ci_fix(factory, rounds=1)
    factory.gh.checks([RUNNING], [RED], [RED])
    answerer = Answerer(factory, "accept")

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert answerer.codes() == [0]
    run_id = run_id_of(result.out)
    assert len(factory.events(run_id, "CiFixStarted")) == 1
    [waiting] = factory.events(run_id, "DecisionWaiting")
    assert (waiting["ticket"], waiting["node"], waiting["cause"]) == ("@run", "ci_failed", "ci_failed")
    card = Path(waiting["card"]).read_text()
    assert f"Round 1: unit: {UNIT}" in card and f"\n- unit: {UNIT}" in card
    assert result.out.splitlines()[-2].endswith("CI failed, accepted by the owner")


def test_run_graph_without_ci_fix_opens_ci_failed_at_once(factory: Harness) -> None:
    spec = ci_run(factory)
    factory.gh.checks([RUNNING], [RED])
    factory.gh.log(91, LOG)
    answerer = Answerer(factory, "accept")

    result = factory.run(spec)

    assert answerer.codes() == [0]
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "CiFixStarted") == []
    assert len(factory.events(run_id, "DecisionWaiting")) == 1


@pytest.mark.parametrize(
    ("script", "reason"),
    [
        (
            AgentScript(files={".github/workflows/ci.yml": "on: push\n"}, output=FIXED),
            ".github/workflows/ci.yml is CI config",
        ),
        (AgentScript(remove=[TEST_FILE], output=FIXED), f"{TEST_FILE} is a deleted test file"),
        (
            AgentScript(files={TEST_FILE: "def test_other():\n    assert 1 == 1\n"}, output=FIXED),
            f"{TEST_FILE} deletes test test_run_lasts_0s",
        ),
        (
            AgentScript(files={TEST_FILE: "import pytest\n\n@pytest.mark.skip\n" + TEST}, output=FIXED),
            f"{TEST_FILE} adds a skip or xfail marker: @pytest.mark.skip",
        ),
        (
            AgentScript(files={TEST_FILE: "import pytest\n\n@pytest.mark.xfail\n" + TEST}, output=FIXED),
            f"{TEST_FILE} adds a skip or xfail marker: @pytest.mark.xfail",
        ),
    ],
)
def test_fix_that_changes_ci_config_or_deletes_skips_or_xfails_a_test_fails_its_round(
    factory: Harness, script: AgentScript, reason: str
) -> None:
    spec = with_ci_fix(factory, rounds=1)
    factory.executor.scripts["implement"] = AgentScript(
        files={TEST_FILE: TEST}, output={"summary": "Add a test", "handoff": "added"}
    )
    factory.executor.scripts["ci_fix"] = script
    factory.gh.checks([RUNNING], [RED], [RED])
    answerer = Answerer(factory, "accept")

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert answerer.codes() == [0]
    run_id = run_id_of(result.out)
    [report] = factory.events(run_id, "CiFixFinished")
    assert (report["outcome"], report["cause"]) == ("failed", "guard")
    assert reason in report["reason"]
    [guard] = [e for e in factory.events(run_id, "CiFixGateFinished") if e["node"] == "guard"]
    assert guard["outcome"] == "fail"
    pushed = factory.events(run_id, "IntegrationBranchPushed")
    assert len(pushed) == 1 and factory.remote_branches()[f"factory/{run_id}"] == pushed[0]["commit"]
    assert len(factory.events(run_id, "DecisionWaiting")) == 1


def test_fixer_that_changes_nothing_fails_its_round(factory: Harness) -> None:
    spec = with_ci_fix(factory, rounds=1)
    factory.executor.scripts["ci_fix"] = AgentScript(output=FIXED)
    factory.gh.checks([RUNNING], [RED], [RED])
    answerer = Answerer(factory, "accept")

    result = factory.run(spec)

    assert answerer.codes() == [0]
    run_id = run_id_of(result.out)
    [report] = factory.events(run_id, "CiFixFinished")
    assert (report["cause"], report["reason"]) == ("no_change", "the fixer changed nothing")
    [waiting] = factory.events(run_id, "DecisionWaiting")
    assert Path(waiting["card"]).read_text().count(f"unit: {UNIT}") == 1


def test_fixer_that_fails_every_attempt_fails_its_round_with_the_last_cause(factory: Harness) -> None:
    spec = with_ci_fix(factory, rounds=1, attempts=2)
    factory.executor.scripts["ci_fix"] = AgentScript(fail="crash", files={"junk.txt": "x\n"})
    factory.gh.checks([RUNNING], [RED], [RED])
    answerer = Answerer(factory, "accept")

    result = factory.run(spec)

    assert answerer.codes() == [0]
    run_id = run_id_of(result.out)
    [report] = factory.events(run_id, "CiFixFinished")
    assert (report["outcome"], report["cause"]) == ("failed", "executor_crash")
    assert [e["outcome"] for e in fixer(factory, run_id, "AttemptFinished")] == ["failed", "failed"]
    assert "junk.txt" in (factory.run_dir(run_id) / "ci_fix" / "round-1" / "attempt-1" / "discarded.patch").read_text()


def test_a_stage_that_routes_done_straight_to_ci_pushes_its_fix_without_a_gate(factory: Harness) -> None:
    spec = with_ci_fix(factory)
    run_graph_with_ci_fix(
        factory,
        {"kind": "ci_fix", "role": "ci-fixer", "rounds": {"max": 2}, "next": {"done": "ci"}},
        {},
    )
    factory.gh.checks([RUNNING], [RED], [PASS])

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [report] = factory.events(run_id, "CiFixFinished")
    assert report["outcome"] == "pushed"
    assert factory.remote_branches()[f"factory/{run_id}"] == report["pushed"]
    assert [e["outcome"] for e in factory.events(run_id, "RunNodeEnded")][-3:] == ["red", "done", "green"]


def test_a_stage_out_of_rounds_with_no_route_ends_the_run_through_escalate_to(factory: Harness) -> None:
    spec = with_ci_fix(factory)
    run_graph_with_ci_fix(
        factory,
        {"kind": "ci_fix", "role": "ci-fixer", "rounds": {"max": 1}, "next": {"done": "ci"}},
        {},
    )
    factory.gh.checks([RUNNING], [RED], [RED])

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "RunNodeEnded")[-1]["next"] == "ended"
    assert factory.events(run_id, "DecisionWaiting") == []
    assert ", CI failed" in result.out.splitlines()[-2]


def test_a_failed_gate_that_routes_away_from_the_stage_leaves_the_fix_unpushed(factory: Harness) -> None:
    spec = with_ci_fix(factory)
    run_graph_with_ci_fix(
        factory,
        {"kind": "ci_fix", "role": "ci-fixer", "rounds": {"max": 2}, "next": {"done": "ci_build"}},
        {"ci_build": {"kind": "command", "with": {"run": "false"}, "next": {"pass": "ci", "fail": "ci_failed"}}},
    )
    factory.gh.checks([RUNNING], [RED])
    answerer = Answerer(factory, "accept")

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert answerer.codes() == [0]
    run_id = run_id_of(result.out)
    [report] = factory.events(run_id, "CiFixFinished")
    assert (report["outcome"], report["cause"]) == ("failed", "gate")
    assert len(factory.events(run_id, "IntegrationBranchPushed")) == 1


def test_a_gate_with_from_reads_the_probe_of_the_gate_before_it_in_the_round(factory: Harness) -> None:
    spec = with_ci_fix(factory)
    run_graph_with_ci_fix(
        factory,
        {
            "kind": "ci_fix",
            "role": "ci-fixer",
            "rounds": {"max": 1, "then": "ci_failed"},
            "next": {"done": "probe"},
        },
        {
            "probe": {
                "kind": "tests",
                "with": {
                    "run": 'echo \'<testsuite tests="1"><testcase name="a"/></testsuite>\' > $GATE_OUT/junit.xml',
                    "junit": "junit.xml",
                },
                "next": {"pass": "judge", "fail": "ci_failed"},
            },
            "judge": {
                "kind": "coverage",
                "from": "probe",
                "with": {"format": "lcov", "report": "lcov.info", "min_changed": 80},
                "next": {"pass": "ci", "fail": "ci_failed"},
            },
        },
    )
    factory.gh.checks([RUNNING], [RED])
    answerer = Answerer(factory, "accept")

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert answerer.codes() == [0]
    run_id = run_id_of(result.out)
    judged = [e for e in factory.events(run_id, "CiFixGateFinished") if e["node"] == "judge"]
    assert [(e["outcome"], e["from"], e["probe"]) for e in judged] == [("fail", "probe", 1)]
    assert Path(judged[0]["verdict"]).parent.name == "from-probe-probe-1"


def test_a_probe_the_sandbox_loses_fails_the_round_with_its_cause_and_the_next_round_passes(factory: Harness) -> None:
    spec = with_ci_fix(factory)
    factory.sandbox.lost_probes = 1
    factory.gh.checks([RUNNING], [RED], [PASS])

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    lost, pushed = factory.events(run_id_of(result.out), "CiFixFinished")
    assert (lost["outcome"], lost["cause"]) == ("failed", "sandbox_lost")
    assert pushed["outcome"] == "pushed"


def test_a_fix_whose_push_fails_ends_its_round_and_the_next_wait_reads_the_old_head(factory: Harness) -> None:
    spec = with_ci_fix(factory, rounds=1)
    factory.gh.checks([RUNNING], [RED], [RED])
    factory.executor.on_start = lambda request: factory.reject_pushes() if request.stage == "ci_fix" else None
    answerer = Answerer(factory, "accept")

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert answerer.codes() == [0]
    run_id = run_id_of(result.out)
    [report] = factory.events(run_id, "CiFixFinished")
    assert (report["outcome"], report["cause"]) == ("failed", "push")
    first, second = [e["commit"] for e in factory.events(run_id, "CiStarted")]
    assert first == second
    assert len(factory.events(run_id, "IntegrationBranchPushed")) == 1


def crash_when_events(factory: Harness, event: str) -> None:
    """Crashes the Run at its next sleep once the Journal holds `event`, as `kill -9` would."""

    def on_sleep(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], event):
            factory.clock.on_sleep = None
            raise Crash

    factory.clock.on_sleep = on_sleep


def test_a_run_killed_while_the_fixer_works_closes_that_round_on_resume_and_takes_the_next(factory: Harness) -> None:
    spec = with_ci_fix(factory)
    factory.gh.checks([RUNNING], [RED], [PASS])
    crashes = [True]

    def crash_once(request: object) -> None:
        if crashes.pop() if crashes else False:
            raise Crash

    factory.executor.on_start = lambda request: crash_once(request) if request.stage == "ci_fix" else None
    with pytest.raises(Crash):
        factory.run(spec)
    factory.crashed()
    factory.clock.sleep(STALE.seconds)
    run_id = factory.run_ids()[-1]

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    lost, pushed = factory.events(run_id, "CiFixFinished")
    assert (lost["round"], lost["outcome"], lost["cause"]) == (1, "failed", "lost")
    assert (pushed["round"], pushed["outcome"]) == (2, "pushed")
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]


def test_a_run_killed_in_a_gate_runs_the_gate_again_on_resume_and_keeps_the_fix(factory: Harness) -> None:
    spec = with_ci_fix(factory)
    factory.gh.checks([RUNNING], [RED], [PASS])
    released = factory.home / "gate-released"
    gate = {"kind": "command", "with": {"run": f"until [ -e {released} ]; do sleep 0.01; done"}}
    run_graph_with_ci_fix(
        factory,
        {"kind": "ci_fix", "role": "ci-fixer", "rounds": {"max": 2}, "next": {"done": "ci_build"}},
        {"ci_build": {**gate, "next": {"pass": "ci", "fail": "ci_fix"}}},
    )

    def crash_in_the_gate(seconds: float) -> None:
        entered = factory.run_ids() and factory.events(factory.run_ids()[-1], "RunNodeEntered")
        if entered and entered[-1]["node"] == "ci_build":
            released.touch()
            factory.clock.on_sleep = None
            raise Crash

    factory.clock.on_sleep = crash_in_the_gate
    with pytest.raises(Crash):
        factory.run(spec)
    factory.crashed()
    factory.clock.sleep(STALE.seconds)
    run_id = factory.run_ids()[-1]
    assert "Run node: ci_build" in factory.cli("status", run_id).out.splitlines()

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert len(factory.events(run_id, "CiFixStarted")) == 1
    [report] = factory.events(run_id, "CiFixFinished")
    assert report["outcome"] == "pushed"
    gate_finished = [e for e in factory.events(run_id, "CiFixGateFinished") if e["node"] == "ci_build"]
    assert [e["probe"] for e in gate_finished] == [2]
    assert Path(gate_finished[0]["verdict"]).parent.name == "probe-2"
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]


def test_a_run_killed_in_the_decision_node_goes_on_asking_on_resume(factory: Harness) -> None:
    spec = ci_run(factory)
    factory.gh.checks([RUNNING], [RED])
    factory.gh.log(91, LOG)
    crash_when_events(factory, "DecisionWaiting")
    with pytest.raises(Crash):
        factory.run(spec)
    factory.crashed()
    factory.clock.sleep(STALE.seconds)
    run_id = factory.run_ids()[-1]
    factory.clock.on_sleep = lambda seconds: (
        answerer.append(factory.spawn("decide", run_id, "@run", "accept")) if not answerer else None
    )
    answerer: list[object] = []

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert len(factory.events(run_id, "DecisionWaiting")) == 1
    assert [e["outcome"] for e in factory.events(run_id, "DecisionDecided")] == ["accept"]
    assert factory.events(run_id, "RunNodeEnded")[-1]["outcome"] == "accept"


def test_a_run_graph_that_routes_red_to_its_end_ends_the_run_without_a_fixer(factory: Harness) -> None:
    spec = ci_run(factory)
    graph = copy.deepcopy(NO_RUN_STAGES)
    del graph["nodes"]["ci_failed"]
    graph["nodes"]["ci"]["next"] = {"green": "ended", "absent": "ended", "red": "ended", "timeout": "ended"}
    put_graph(factory, "red-ends", graph)
    factory.gh.checks([RUNNING], [RED])
    factory.gh.log(91, LOG)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "CiFixStarted") == []
    assert factory.events(run_id, "DecisionWaiting") == []
    assert factory.events(run_id, "RunNodeEnded")[-1]["outcome"] == "red"
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]
    assert result.out.splitlines()[-2].endswith(", CI failed")


def test_the_default_run_graph_waits_for_ci_fixes_red_ci_and_asks_the_owner(factory: Harness) -> None:
    spec = ci_run(factory)
    del factory.project["run"]
    factory.executor.scripts["describe"] = DESCRIPTION
    factory.executor.scripts["ci_fix"] = AgentScript(output=FIXED)
    factory.gh.checks([RUNNING], [RED], [RED])
    factory.gh.log(91, LOG)
    answerer = Answerer(factory, "accept")

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert answerer.codes() == [0]
    run_id = run_id_of(result.out)
    assert [(e["node"], e["outcome"]) for e in factory.events(run_id, "RunNodeEnded")] == [
        ("select", "selected"),
        ("build", "all_merged"),
        ("describe", "done"),
        ("open_pr", "opened"),
        ("ci", "red"),
        ("ci_fix", "failed"),
        ("ci_fix", "failed"),
        ("ci_fix", "exhausted"),
        ("ci_failed", "accept"),
    ]
    assert [e["round"] for e in factory.events(run_id, "CiFixFinished")] == [1, 2]
    assert result.out.splitlines()[-2].endswith("CI failed, accepted by the owner")


def test_check_names_the_run_graph_node_that_replaces_the_ci_fix_field(factory: Harness) -> None:
    ci_run(factory)
    factory.pipeline["ci_fix"] = {"role": "ci-fixer", "gates": ["build"], "rounds": 0}

    result = factory.check()

    assert result.code == 2
    assert (
        "ci_fix: is now the node `ci_fix` of the Run graph, with its Gates as nodes after it; "
        "set its fields under `overrides.nodes.ci_fix` of the project file"
    ) in result.err


def test_check_requires_the_ci_fix_schema_to_output_what_the_pr_line_reads(factory: Harness) -> None:
    with_ci_fix(factory, role="implementer")

    result = factory.check()

    assert result.code == 2
    assert "nodes.ci_fix: Role implementer: the schema of the CI fixer must require failure" in result.err


STAGE = {"kind": "ci_fix", "role": "ci-fixer", "next": {"done": "ci"}}


@pytest.mark.parametrize(
    ("change", "error"),
    [
        pytest.param(
            {"ci_fix": {**STAGE, "workspace": "integration"}},
            "nodes.ci_fix.workspace: is retired, as the kind says what a Stage commits",
            id="workspace",
        ),
        pytest.param(
            {"ci_fix": {**STAGE, "rounds": {"max": 0}}},
            "nodes.ci_fix.rounds.max: must be a positive integer",
            id="rounds",
        ),
        pytest.param(
            {
                "ci_fix": {**STAGE, "rounds": {"max": 1}},
                "describe": {"kind": "describe", "role": "describer", "rounds": {"max": 1}},
            },
            "nodes.describe.rounds: a Stage of kind describe has no rounds",
            id="describer-rounds",
        ),
        pytest.param(
            {"ci_fix": STAGE},
            "nodes.ci: outcome red loops back to this node, and the Run would never end",
            id="no-rounds-loop",
        ),
        pytest.param(
            {"ci_fix": {**STAGE, "rounds": {"max": 1}}, "other": {**STAGE, "rounds": {"max": 1}}},
            "nodes: a Run graph has at most one node of kind ci_fix, and it has ci_fix, other",
            id="two-stages",
        ),
        pytest.param(
            {
                "ci_fix": {**STAGE, "rounds": {"max": 1}},
                "ci_failed": {
                    "decider": {"kind": "model", "role": "ci-fixer"},
                    "outcomes": ["accept"],
                    "next": {"accept": "ended"},
                },
            },
            "nodes.ci_failed: a Decision of a Run graph has only a human Decider",
            id="model-decider",
        ),
        pytest.param(
            {
                "ci_fix": {**STAGE, "rounds": {"max": 1}},
                "ci_failed": {
                    "decider": {"kind": "human"},
                    "outcomes": ["accept"],
                    "inputs": ["spec"],
                    "next": {"accept": "ended"},
                },
            },
            "nodes.ci_failed.inputs: a Decision of a Run graph reads no inputs",
            id="decision-inputs",
        ),
        pytest.param(
            {
                "ci_fix": {**STAGE, "rounds": {"max": 1}, "next": {"done": "ci"}},
                "stray": {"kind": "command", "with": {"run": "make"}, "next": {"pass": "ci", "fail": "ended"}},
                "ci_failed": {
                    "decider": {"kind": "human"},
                    "outcomes": {"accept": ""},
                    "next": {"accept": "stray"},
                },
            },
            "nodes.stray: a Run Gate judges a fix, so only done of a Stage whose kind commits or pass of a Run Gate may"
            " lead here, and accept of ci_failed does",
            id="stray-gate",
        ),
    ],
)
def test_check_refuses_what_a_run_graph_says_of_its_ci_fix(
    factory: Harness, change: dict[str, Any], error: str
) -> None:
    ci_run(factory)
    graph = copy.deepcopy(NO_RUN_STAGES)
    graph["nodes"]["ci"]["next"]["red"] = "ci_fix"
    graph["nodes"] |= change
    path = put_graph(factory, "broken", graph)

    result = factory.check()

    assert result.code == 2
    assert f"{display_path(path)}: {error}" in result.err


def test_check_refuses_a_ci_fix_or_a_decision_before_the_ci_of_the_pr(factory: Harness) -> None:
    ci_run(factory)
    graph = copy.deepcopy(NO_RUN_STAGES)
    graph["nodes"]["open_pr"]["next"]["opened"] = "ci_fix"
    graph["nodes"]["open_pr"]["next"]["failed"] = "ci_failed"
    graph["nodes"]["ci_fix"] = {**STAGE, "rounds": {"max": 1}}
    put_graph(factory, "early", graph)

    result = factory.check()

    assert result.code == 2
    assert "nodes.ci_fix: a path reaches this Stage before the wait_ci node" in result.err
    assert "nodes.ci_failed: a path reaches this Decision before the wait_ci node" in result.err


def test_the_shipped_tdd_review_run_graph_judges_the_fix_with_build_and_tests(factory: Harness) -> None:
    spec = ci_run(factory)
    factory.project["run"] = "tdd-review"
    factory.project["vars"] = {
        "build": "true",
        "test": 'echo \'<testsuite tests="1"><testcase name="a"/></testsuite>\' > $GATE_OUT/junit.xml',
        "junit": "junit.xml",
    }
    factory.executor.scripts["describe"] = DESCRIPTION
    factory.executor.scripts["ci_fix"] = FIX
    factory.gh.checks([RUNNING], [RED], [PASS])
    factory.gh.log(91, LOG)

    assert factory.check().code == 0
    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    judged = factory.events(run_id, "CiFixGateFinished")
    assert [(e["node"], e["outcome"]) for e in judged] == [
        ("guard", "pass"),
        ("ci_build", "pass"),
        ("ci_tests", "pass"),
    ]
    assert [e["outcome"] for e in factory.events(run_id, "CiFixFinished")] == ["pushed"]
