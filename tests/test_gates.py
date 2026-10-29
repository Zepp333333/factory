import json
from pathlib import Path
from typing import Any

import pytest

from harness import AgentScript, Harness, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})
NOTHING = AgentScript(output={"handoff": "nothing to do"})


def with_gate(factory: Harness, fail: str = "fix", **gate: Any) -> None:
    """Puts the Gate `build` between `implement` and `merge`, with a `fix` Stage that leads back to it."""
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["stages"]["fix"] = {"role": "implementer", "inputs": ["ticket"], "next": {"done": "build"}}
    factory.pipeline["gates"] = {
        "build": {"kind": "command", "with": {"run": "test -f hello.txt"}, "next": {"pass": "merge", "fail": fail}}
    }
    factory.pipeline["gates"]["build"].update(gate)


def nodes(factory: Harness, run_id: str) -> list[str]:
    return [e["node"] for e in factory.events(run_id, "NodeEntered")]


def verdicts(factory: Harness, run_id: str) -> list[dict[str, Any]]:
    return [json.loads(Path(e["verdict"]).read_text()) for e in factory.events(run_id, "GateFinished")]


def test_passing_gate_sends_the_ticket_on_by_next(factory: Harness) -> None:
    with_gate(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert nodes(factory, run_id) == ["implement", "build", "merge", "done"]
    [verdict] = verdicts(factory, run_id)
    assert set(verdict) == {"gate", "kind", "outcome", "reasons", "summary", "log", "exit_code", "duration"}
    assert (verdict["gate"], verdict["kind"], verdict["outcome"], verdict["exit_code"]) == (
        "build",
        "command",
        "pass",
        0,
    )


def test_failing_gate_sends_the_ticket_to_fix_and_judges_again(factory: Harness) -> None:
    with_gate(factory)
    factory.pipeline["gates"]["build"]["with"] = {"run": "echo 'error: no hello.txt' >&2; test -f hello.txt"}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert nodes(factory, run_id) == ["implement", "build", "fix", "build", "merge", "done"]
    failed, passed = verdicts(factory, run_id)
    assert (failed["outcome"], failed["reasons"], failed["exit_code"]) == ("fail", ["exit code 1"], 1)
    assert failed["summary"]["matched"] == ["error: no hello.txt"]
    assert Path(failed["log"]).read_text() == "error: no hello.txt\n"
    assert passed["outcome"] == "pass"


def test_gate_that_runs_past_its_timeout_fails(factory: Harness) -> None:
    with_gate(factory, fail="abandoned")
    factory.pipeline["gates"]["build"]["with"] = {"run": "sleep 30", "timeout": "2s"}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    assert nodes(factory, run_id) == ["implement", "build", "abandoned"]
    [verdict] = verdicts(factory, run_id)
    assert (verdict["outcome"], verdict["reasons"], verdict["exit_code"]) == ("fail", ["timed out after 2s"], None)


def test_report_committed_by_the_agent_never_reaches_gate_out(factory: Harness) -> None:
    with_gate(factory, fail="abandoned")
    factory.pipeline["gates"]["build"]["with"] = {
        "run": 'test -z "$(ls -A "$GATE_OUT")" && echo green > "$GATE_OUT/report.txt"'
    }
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={"report.txt": "green\n", "gate_out/report.txt": "green\n", ".factory/gate_out/report.txt": "green\n"},
        output={"handoff": "faked a report"},
    )

    result = factory.run(spec)

    assert result.code == 0, result.out
    [started] = factory.events(run_id_of(result.out), "ProbeStarted")
    gate_out = Path(started["gate_out"])
    assert [p.name for p in gate_out.iterdir()] == ["report.txt"]
    assert not gate_out.is_relative_to(factory.workspace(run_id_of(result.out), "t1"))


@pytest.mark.parametrize(("clean", "outcome"), [(True, "pass"), (False, "fail")])
def test_clean_removes_ignored_files_before_the_probe(factory: Harness, clean: bool, outcome: str) -> None:
    with_gate(factory, fail="abandoned", clean=clean)
    factory.project["caches"] = ["build"]
    factory.pipeline["gates"]["build"]["with"] = {"run": "test ! -e build/planted.bin"}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={".gitignore": "build/\n", "build/planted.bin": "fake binary\n"}, output={"handoff": "done"}
    )

    run_id = run_id_of(factory.run(spec).out)

    [verdict] = verdicts(factory, run_id)
    assert verdict["outcome"] == outcome


def test_probe_leaves_nothing_behind_for_the_next_stage_commit(factory: Harness) -> None:
    with_gate(factory)
    factory.pipeline["gates"]["build"]["with"] = {
        "run": "echo leftover > probe.txt; echo x >> README.md; test -f hello.txt"
    }
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out
    integration = f"factory/{run_id_of(result.out)}"
    assert factory.git("ls-tree", "--name-only", integration).splitlines() == ["README.md", "factory.yaml", "hello.txt"]
    assert factory.git("show", f"{integration}:README.md") == "# Target"


def test_lost_probe_retries_the_gate_without_spending_a_round(factory: Harness) -> None:
    with_gate(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO
    factory.sandbox.lost_probes = 1

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert nodes(factory, run_id) == ["implement", "build", "merge", "done"]
    [lost] = factory.events(run_id, "ProbeLost")
    assert (lost["node"], lost["attempt"]) == ("build", 1)
    assert "did not start" in lost["reason"]
    [verdict] = verdicts(factory, run_id)
    assert verdict["outcome"] == "pass"


def test_gate_escalates_once_the_ticket_lost_its_sandbox_more_often_than_sandbox_retries(factory: Harness) -> None:
    with_gate(factory)
    factory.pipeline["defaults"]["sandbox_retries"] = 1
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO
    factory.sandbox.lost_probes = 2

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    assert nodes(factory, run_id) == ["implement", "build", "abandoned"]
    assert [e["probe"] for e in factory.events(run_id, "ProbeLost")] == [1, 2]
    assert factory.events(run_id, "GateFinished") == []
    assert factory.cli("status", run_id).out.splitlines()[2].split()[:7] == [
        "t1",
        "abandoned",
        "implement",
        "1",
        "1",
        "2",
        "abandoned",
    ]


def test_gate_reaches_a_verdict_after_as_many_lost_probes_as_sandbox_retries(factory: Harness) -> None:
    with_gate(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO
    factory.sandbox.lost_probes = 2

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert len(factory.events(run_id, "ProbeLost")) == 2
    assert [e["attempt"] for e in factory.events(run_id, "ProbeStarted")] == [1, 2, 3]
    assert factory.cli("status", run_id).out.splitlines()[2].split()[5] == "2"


FIXTURE = "tests/fixtures/gates/gate_out/coverage.xml"


def ignore_on_main(factory: Harness, rules: str) -> None:
    (factory.repo / ".gitignore").write_text(rules)
    factory.git("add", ".gitignore")
    factory.git("commit", "-q", "-m", "Ignore reports")


def test_stage_that_writes_a_file_under_gitignore_fails_naming_the_file(factory: Harness) -> None:
    ignore_on_main(factory, "coverage.xml\n")
    with_gate(factory, fail="abandoned")
    factory.sandbox.refuse_remove = True
    factory.pipeline["gates"]["build"]["with"] = {"run": f"test -f {FIXTURE}"}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n", FIXTURE: "<coverage/>\n"}, output={"summary": "Add fixture", "handoff": "done"}
    )

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "GateFinished") == []
    [failed] = [e for e in factory.events(run_id, "AttemptFinished") if e["outcome"] == "failed"]
    assert failed["cause"] == "ignored_files"
    assert failed["errors"] == [f"{FIXTURE} is ignored by .gitignore, so the Stage boundary commit leaves it out"]
    assert failed["removed_ignored"] == [FIXTURE]
    workspace = factory.workspace(run_id, "t1")
    assert workspace.is_dir()
    assert not (workspace / "tests").exists()


def test_caches_the_project_lists_are_no_stray_ignored_files(factory: Harness) -> None:
    with_gate(factory)
    factory.project["caches"] = ["__pycache__", ".pytest_cache", ".venv"]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={
            "hello.txt": "hello\n",
            ".gitignore": "__pycache__/\n.pytest_cache/\n.venv/\n",
            "pkg/__pycache__/a.pyc": "x",
            ".pytest_cache/v": "x",
            ".venv/bin/python": "x",
        },
        output={"summary": "Add hello.txt", "handoff": "done"},
    )

    result = factory.run(spec)

    assert result.code == 0, result.out


def test_ignored_nested_repository_is_a_stray_the_host_removes(factory: Harness) -> None:
    ignore_on_main(factory, "vendor/\n")
    with_gate(factory, fail="abandoned")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n", "vendor/lib/a.txt": "a\n"},
        run=["git -C vendor/lib init -q"],
        output={"summary": "Vendor lib", "handoff": "done"},
    )

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    [failed] = [e for e in factory.events(run_id, "AttemptFinished") if e["outcome"] == "failed"]
    assert failed["cause"] == "ignored_files"
    assert failed["removed_ignored"] == ["vendor/lib/"]


def test_stray_ignored_files_of_a_failed_attempt_are_removed_and_recorded(factory: Harness) -> None:
    ignore_on_main(factory, "*.log\n")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"logs/run.log": "x\n"}, fail="crash")

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    [failed] = factory.events(run_id, "AttemptFinished")
    assert failed["cause"] == "executor_crash"
    assert failed["removed_ignored"] == ["logs/run.log"]


def test_read_only_stage_that_leaves_an_ignored_file_is_a_violation(factory: Harness) -> None:
    ignore_on_main(factory, "*.xml\n")
    factory.pipeline["stages"]["implement"]["next"]["done"] = "review"
    factory.pipeline["stages"]["review"] = {
        "role": "implementer",
        "workspace": "ro",
        "inputs": ["ticket"],
        "next": {"done": "merge"},
    }
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO
    factory.executor.scripts["review"] = AgentScript(
        files={"report.xml": "<r/>\n"}, output={"summary": "Looks fine", "handoff": "ok"}
    )

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [violation] = factory.events(run_id, "WorkspaceViolated")
    assert violation["removed_ignored"] == ["report.xml"]


def test_gate_that_escalates_names_the_last_lost_probe_in_the_journal_and_in_status(factory: Harness) -> None:
    with_gate(factory)
    factory.pipeline["defaults"]["sandbox_retries"] = 1
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO
    factory.sandbox.lost_probes = 2

    run_id = run_id_of(factory.run(spec).out)

    [exhausted] = factory.events(run_id, "SandboxRetriesExhausted")
    assert (exhausted["node"], exhausted["losses"], exhausted["sandbox_retries"]) == ("build", 2, 1)
    assert (exhausted["cause"], "did not start" in exhausted["reason"]) == ("probe_lost", True)
    lines = factory.cli("status", run_id).out.splitlines()
    assert f"Ticket t1 lost its Sandbox 2x, last: probe of build: {exhausted['reason']}" in lines


def test_lost_attempt_and_lost_probe_of_one_ticket_count_together(factory: Harness) -> None:
    with_gate(factory)
    factory.pipeline["defaults"]["sandbox_retries"] = 1
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = HELLO
    factory.sandbox.lost_probes = 1
    factory.sandbox.refuse, factory.sandbox.refuse_node = 1, "fix"

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    [lost] = factory.events(run_id, "ProbeLost")
    assert lost["node"] == "build"
    [finished] = [e for e in factory.events(run_id, "AttemptFinished") if e["node"] == "fix"]
    assert (finished["round"], finished["cause"]) == (1, "sandbox_start")
    [exhausted] = factory.events(run_id, "SandboxRetriesExhausted")
    assert (exhausted["node"], exhausted["losses"], exhausted["cause"]) == ("fix", 2, "sandbox_start")
    row = factory.cli("status", run_id).out.splitlines()[2].split()
    assert row[:2] == ["t1", "abandoned"] and row[5] == "2"
