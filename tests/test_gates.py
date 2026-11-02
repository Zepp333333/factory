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


def test_passing_gate_sends_the_ticket_on_after_lost_probes_and_reads_only_what_its_probe_wrote(
    factory: Harness,
) -> None:
    """The Sandbox loses two Probes, as many as `sandbox_retries` allows, and the third reaches a verdict.

    The agent commits fake reports, and the Probe sees an empty `$GATE_OUT` outside the Workspace.
    """
    with_gate(factory)
    factory.pipeline["gates"]["build"]["with"] = {
        "run": 'test -z "$(ls -A "$GATE_OUT")" && echo green > "$GATE_OUT/report.txt" && test -f hello.txt'
    }
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={
            "hello.txt": "hello\n",
            "report.txt": "green\n",
            "gate_out/report.txt": "green\n",
            ".factory/gate_out/report.txt": "green\n",
        },
        output={"summary": "Add hello.txt", "handoff": "done"},
    )
    factory.sandbox.lost_probes = 2

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert nodes(factory, run_id) == ["implement", "build", "merge", "done"]
    lost = factory.events(run_id, "ProbeLost")
    assert [(e["node"], e["attempt"], e["probe"]) for e in lost] == [("build", 1, 1), ("build", 2, 2)]
    assert all("did not start" in e["reason"] for e in lost)
    assert [e["attempt"] for e in factory.events(run_id, "ProbeStarted")] == [1, 2, 3]
    *_, started = factory.events(run_id, "ProbeStarted")
    gate_out = Path(started["gate_out"])
    assert [p.name for p in gate_out.iterdir()] == ["report.txt"]
    assert not gate_out.is_relative_to(factory.workspace(run_id, "t1"))
    [verdict] = verdicts(factory, run_id)
    assert set(verdict) == {"gate", "kind", "outcome", "reasons", "summary", "log", "exit_code", "duration"}
    assert (verdict["gate"], verdict["kind"], verdict["outcome"], verdict["exit_code"]) == (
        "build",
        "command",
        "pass",
        0,
    )
    assert factory.cli("status", run_id).out.splitlines()[2].split()[5] == "2"


def test_failing_gate_sends_the_ticket_to_fix_and_judges_again_and_its_probe_leaves_nothing_behind(
    factory: Harness,
) -> None:
    with_gate(factory)
    factory.pipeline["gates"]["build"]["with"] = {
        "run": "echo leftover > probe.txt; echo x >> README.md; echo 'error: no hello.txt' >&2; test -f hello.txt"
    }
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
    integration = f"factory/{run_id}"
    assert factory.git("ls-tree", "--name-only", integration).splitlines() == ["README.md", "factory.yaml", "hello.txt"]
    assert factory.git("show", f"{integration}:README.md") == "# Target"


@pytest.mark.parametrize(("clean", "outcome"), [(True, "pass"), (False, "fail")])
def test_caches_the_project_lists_are_no_stray_files_and_clean_removes_them_before_the_probe(
    factory: Harness, clean: bool, outcome: str
) -> None:
    with_gate(factory, fail="abandoned", clean=clean)
    factory.project["caches"] = ["build", "__pycache__", ".pytest_cache", ".venv"]
    factory.pipeline["gates"]["build"]["with"] = {"run": "test ! -e build/planted.bin"}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={
            ".gitignore": "build/\n__pycache__/\n.pytest_cache/\n.venv/\n",
            "build/planted.bin": "fake binary\n",
            "pkg/__pycache__/a.pyc": "x",
            ".pytest_cache/v": "x",
            ".venv/bin/python": "x",
        },
        output={"handoff": "done"},
    )

    run_id = run_id_of(factory.run(spec).out)

    [attempt] = factory.events(run_id, "AttemptFinished")
    assert attempt["outcome"] == "done"
    [verdict] = verdicts(factory, run_id)
    assert verdict["outcome"] == outcome


def test_gate_escalates_once_the_ticket_lost_its_sandbox_more_often_than_sandbox_retries_naming_the_last_loss(
    factory: Harness,
) -> None:
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
    [exhausted] = factory.events(run_id, "SandboxRetriesExhausted")
    assert (exhausted["node"], exhausted["losses"], exhausted["sandbox_retries"]) == ("build", 2, 1)
    assert (exhausted["cause"], "did not start" in exhausted["reason"]) == ("probe_lost", True)
    lines = factory.cli("status", run_id).out.splitlines()
    assert f"Ticket t1 lost its Sandbox 2x, last: probe of build: {exhausted['reason']}" in lines


FIXTURE = "tests/fixtures/gates/gate_out/coverage.xml"


def ignore_on_main(factory: Harness, rules: str) -> None:
    (factory.repo / ".gitignore").write_text(rules)
    factory.git("add", ".gitignore")
    factory.git("commit", "-q", "-m", "Ignore reports")


def test_stage_that_writes_files_or_a_nested_repository_under_gitignore_fails_naming_them(factory: Harness) -> None:
    """The Sandbox refuses to be removed, and the host still removes the stray files and the nested repository."""
    ignore_on_main(factory, "coverage.xml\nvendor/\n")
    with_gate(factory, fail="abandoned")
    factory.sandbox.refuse_remove = True
    factory.pipeline["gates"]["build"]["with"] = {"run": f"test -f {FIXTURE}"}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n", FIXTURE: "<coverage/>\n", "vendor/lib/a.txt": "a\n"},
        run=["git -C vendor/lib init -q"],
        output={"summary": "Add fixture", "handoff": "done"},
    )

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "GateFinished") == []
    [failed] = [e for e in factory.events(run_id, "AttemptFinished") if e["outcome"] == "failed"]
    assert failed["cause"] == "ignored_files"
    assert failed["errors"] == [
        f"{FIXTURE} is ignored by .gitignore, so the Stage boundary commit leaves it out",
        "vendor/lib/ is ignored by .gitignore, so the Stage boundary commit leaves it out",
    ]
    assert failed["removed_ignored"] == [FIXTURE, "vendor/lib/"]
    workspace = factory.workspace(run_id, "t1")
    assert workspace.is_dir()
    assert not (workspace / "tests").exists()
    assert not (workspace / "vendor" / "lib").exists()


def test_stray_ignored_files_of_a_failed_attempt_are_removed_and_recorded(factory: Harness) -> None:
    ignore_on_main(factory, "*.log\n")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"logs/run.log": "x\n"}, fail="crash")

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    [failed] = factory.events(run_id, "AttemptFinished")
    assert failed["cause"] == "executor_crash"
    assert failed["removed_ignored"] == ["logs/run.log"]


def test_lost_attempt_and_lost_probe_of_one_ticket_count_together_after_a_probe_that_timed_out(
    factory: Harness,
) -> None:
    """The Sandbox loses the first Probe, the second runs past its timeout and fails, and the fix Stage is lost."""
    with_gate(factory)
    factory.pipeline["gates"]["build"]["with"] = {"run": "test -f hello.txt || sleep 30", "timeout": "2s"}
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
    [verdict] = verdicts(factory, run_id)
    assert (verdict["outcome"], verdict["reasons"], verdict["exit_code"]) == ("fail", ["timed out after 2s"], None)
    [finished] = [e for e in factory.events(run_id, "AttemptFinished") if e["node"] == "fix"]
    assert (finished["round"], finished["cause"]) == (1, "sandbox_start")
    [exhausted] = factory.events(run_id, "SandboxRetriesExhausted")
    assert (exhausted["node"], exhausted["losses"], exhausted["cause"]) == ("fix", 2, "sandbox_start")
    row = factory.cli("status", run_id).out.splitlines()[2].split()
    assert row[:2] == ["t1", "abandoned"] and row[5] == "2"
