import json
from pathlib import Path
from typing import Any

import pytest

from harness import AgentScript, Harness, run_id_of
from test_ci_wait import LOG, PASS, RED, RUNNING, UNIT, Answerer, ci_run

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


def with_ci_fix(factory: Harness, **fields: object) -> Path:
    """Gives the Pipeline a CI fixer whose Gate `build` fails while `broken.txt` exists, and returns the Spec folder."""
    spec = ci_run(factory)
    factory.pipeline["ci_fix"] = {"role": "ci_fixer", "gates": ["build"], **fields}
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {
        "build": {
            "kind": "command",
            "with": {"run": "test ! -f broken.txt"},
            "next": {"pass": "sync", "fail": "abandoned"},
        }
    }
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
    [finished] = factory.events(run_id, "CiFinished")
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
    assert first["reason"].startswith("Gate build failed")
    assert second["outcome"] == "pushed"
    briefs = [e["brief"] for e in fixer(factory, run_id, "AttemptStarted")]
    assert [b["round"] for b in briefs] == [1, 2]
    sources = [(i["source"], i.get("round")) for i in briefs[1]["inputs"]]
    assert sources == [("ci_log", None), ("diff", None), ("stage", 1), ("cause", None), ("gate", 1), ("gate", 1)]
    [verdict] = [
        i for i in briefs[1]["inputs"] if i["source"] == "gate" and i["path"].endswith("build/probe-1/verdict.json")
    ]
    assert json.loads(Path(verdict["path"]).read_text())["outcome"] == "fail"
    assert [e["commit"] for e in factory.events(run_id, "CiStarted")][0] == briefs[1]["head"]
    [pull] = factory.gh.pulls()
    assert "- Round 1: " in pull["body"] and "Not pushed: Gate build failed" in pull["body"]
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


def test_pipeline_without_ci_fix_opens_ci_failed_at_once(factory: Harness) -> None:
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


def test_check_refuses_a_gate_the_pipeline_does_not_declare_and_checks_the_ci_fix_fields(factory: Harness) -> None:
    ci_run(factory)
    factory.pipeline["ci_fix"] = {"role": "ci_fixer", "gates": ["build"], "rounds": 0}

    result = factory.check()

    assert result.code != 0
    assert "ci_fix.gates[0]: names undeclared Gate build" in result.err
    assert "ci_fix.rounds" in result.err


def test_check_requires_the_ci_fixer_schema_to_output_what_the_pr_line_reads(factory: Harness) -> None:
    ci_run(factory)
    factory.pipeline["ci_fix"] = {"role": "implementer"}

    result = factory.check()

    assert result.code != 0
    assert "ci_fix: Role implementer: the schema of the CI fixer must require failure" in result.err
