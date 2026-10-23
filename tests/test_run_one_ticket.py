import json
import os
from pathlib import Path

from harness import AgentScript, Harness, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"


def test_run_lands_the_agent_commit_on_the_integration_branch(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "hello.txt exists"}
    )

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    integration = f"factory/{run_id}"
    assert factory.git("show", f"{integration}:hello.txt") == "hello"
    assert factory.git("log", "-1", "--format=%s", integration) == "Add hello.txt"


def test_stage_boundary_commit_carries_trailers_and_falls_back_to_stage_subject(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})

    run_id = run_id_of(factory.run(spec).out)

    integration = f"factory/{run_id}"
    assert factory.git("log", "-1", "--format=%s", integration) == "implement: t1"
    trailers = factory.git("log", "-1", "--format=%(trailers:only,unfold)", integration).splitlines()
    assert trailers == [
        f"Factory-Run: {run_id}",
        "Factory-Ticket: t1",
        "Factory-Stage: implement",
        "Factory-Attempt: 1",
    ]
    assert factory.git("log", "-1", "--format=%an <%ae>", integration) == "Software Factory <factory@localhost>"


def test_run_leaves_the_owner_branches_alone(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})
    before = factory.branches()

    run_id = run_id_of(factory.run(spec).out)

    after = factory.branches()
    assert {name: after[name] for name in before} == before
    assert all(name.startswith(f"factory/{run_id}") for name in after.keys() - before.keys())
    assert factory.git("status", "--porcelain") == ""


def test_agent_gets_an_immutable_brief_of_pointers_to_files_rendered_from_the_snapshot(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})
    base = factory.git("rev-parse", "main")

    run_id = run_id_of(factory.run(spec).out)

    run_dir = factory.run_dir(run_id)
    assert (run_dir / "spec.md").read_text() == "# Greeting\n\nSay hello.\n"
    assert (run_dir / "tickets" / "t1" / "ticket.md").read_text() == "# Greet\n\nWrite hello.txt.\n"
    [request] = factory.executor.requests
    assert request.brief.is_relative_to(run_dir)
    assert not os.access(request.brief, os.W_OK)
    brief = json.loads(request.brief.read_text())
    assert brief["inputs"] == [
        {"source": "spec", "path": str(run_dir / "spec.md")},
        {"source": "ticket", "path": str(run_dir / "tickets" / "t1" / "ticket.md")},
    ]
    assert brief["base"] == base
    output = Path(brief["output_dir"]) / "output.json"
    assert json.loads(output.read_text()) == {"handoff": "done"}


def test_output_that_breaks_the_role_schema_is_never_committed(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n"}, output={"summary": "no handoff"}
    )

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    assert factory.git("rev-parse", f"factory/{run_id}") == factory.git("rev-parse", "main")
    assert factory.cli("status", run_id).out.splitlines() == [
        f"Run {run_id}  partial  factory/{run_id}",
        "TICKET  NODE       LAST STAGE  ROUND  ATTEMPT  LOST  STATE",
        "t1      abandoned  implement   1      1        0     abandoned",
        f"Ticket t1 abandoned, its work is in refs/factory/{run_id}/abandoned/t1",
    ]


def test_commit_subject_is_the_first_line_of_summary(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    summary = "Add hello.txt\n\nFactory-Ticket: forged"
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n"}, output={"summary": summary, "handoff": "done"}
    )

    run_id = run_id_of(factory.run(spec).out)

    integration = f"factory/{run_id}"
    assert factory.git("log", "-1", "--format=%s", integration) == "Add hello.txt"
    body = factory.git("log", "-1", "--format=%b", integration)
    assert "forged" not in body


def test_attempt_artifact_records_the_commit_range(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})

    run_id = run_id_of(factory.run(spec).out)

    [request] = factory.executor.requests
    attempt = json.loads((request.attempt_dir / "attempt.json").read_text())
    assert attempt["head_before"] == factory.git("rev-parse", "main")
    assert attempt["head_after"] == factory.git("rev-parse", f"factory/{run_id}")
    assert attempt["outcome"] == "done"


def test_run_refuses_a_pipeline_that_routes_to_an_undeclared_node(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.pipeline["stages"]["implement"]["next"] = {"done": "build"}
    before = factory.branches()

    result = factory.run(spec)

    assert result.code == 2
    assert factory.branches() == before


def test_status_shows_each_ticket_with_its_node_round_attempt_and_the_run_state(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})
    run_id = run_id_of(factory.run(spec).out)

    result = factory.cli("status", run_id)

    assert result.code == 0
    assert result.out.splitlines() == [
        f"Run {run_id}  done  factory/{run_id}",
        "TICKET  NODE  LAST STAGE  ROUND  ATTEMPT  LOST  STATE",
        "t1      done  implement   1      1        0     merged",
    ]


def test_status_of_an_unknown_run_fails(factory: Harness) -> None:
    assert factory.cli("status", "nope").code == 2


def test_run_prints_its_id_first_and_then_the_tree_as_it_moves(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})

    out = factory.run(spec).out

    run_id = run_id_of(out)
    lines = out.splitlines()
    assert lines[0] == f"Run {run_id} started on factory/{run_id}"
    assert "  ~ t1           implement    active, implement round 1, attempt 1" in lines
    assert "  ~ t1           merge        active, implement round 1, attempt 1" in lines
    assert f"Run {run_id}  [done]  factory/{run_id}" in lines
    assert "  + t1           done         merged, implement round 1, attempt 1" in lines
    assert f"Run {run_id} finished: done" in lines


def test_ticket_with_broken_front_matter_stops_the_run_before_it_starts(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": "---\ntitle: [Greet\n---\nWrite hello.txt.\n"})
    before = factory.branches()

    result = factory.run(spec)

    assert result.code == 2
    assert result.err.startswith(f"factory: {spec / 'tickets' / 't1.md'}: front matter is not valid YAML")
    assert factory.branches() == before
