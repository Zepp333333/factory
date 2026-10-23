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
    assert factory.cli("status", run_id).out.splitlines()[2].split() == [
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
    assert [e["attempt"] for e in factory.events(run_id, "ProbeStarted")] == [1, 1, 1]
    assert factory.cli("status", run_id).out.splitlines()[2].split()[-2] == "2"
