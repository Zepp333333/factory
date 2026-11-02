import json
import os
import re
from pathlib import Path

from harness import AgentScript, Harness, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
WAVE = "---\ntitle: Wave\n---\nWrite wave.txt.\n"


def test_run_lands_each_agent_commit_with_its_trailers_and_shows_the_tree_and_status(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET, "t2": WAVE})
    factory.executor.ticket_scripts[("t1", "implement")] = AgentScript(
        files={"hello.txt": "hello\n"},
        output={"summary": "Add hello.txt\n\nFactory-Ticket: forged", "handoff": "hello.txt exists"},
    )
    factory.executor.ticket_scripts[("t2", "implement")] = AgentScript(
        files={"wave.txt": "wave\n"}, output={"handoff": "done"}
    )
    base = factory.git("rev-parse", "main")
    before = factory.branches()

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    integration = f"factory/{run_id}"
    run_dir = factory.run_dir(run_id)
    first, second = factory.executor.requests

    # Each Attempt commits once on its Ticket branch, and the merges bring both commits onto the Integration branch.
    attempts = [json.loads((r.attempt_dir / "attempt.json").read_text()) for r in (first, second)]
    assert [a["outcome"] for a in attempts] == ["done", "done"]
    assert attempts[0]["head_before"] == base
    assert attempts[1]["head_after"] == factory.git("rev-parse", integration)
    assert factory.git("show", f"{integration}:hello.txt") == "hello"
    assert factory.git("show", f"{integration}:wave.txt") == "wave"

    # The subject is the first line of the summary, and without a summary it falls back to the Stage and Ticket.
    hello, wave = (a["head_after"] for a in attempts)
    assert factory.git("log", "-1", "--format=%s", hello) == "Add hello.txt"
    assert "forged" not in factory.git("log", "-1", "--format=%b", hello)
    assert factory.git("log", "-1", "--format=%s", wave) == "implement: t2"
    for commit, ticket in ((hello, "t1"), (wave, "t2")):
        assert factory.git("log", "-1", "--format=%(trailers:only,unfold)", commit).splitlines() == [
            f"Factory-Run: {run_id}",
            f"Factory-Ticket: {ticket}",
            "Factory-Stage: implement",
            "Factory-Attempt: 1",
        ]
        assert factory.git("log", "-1", "--format=%an <%ae>", commit) == "Software Factory <factory@localhost>"

    # The owner's branches and working tree stay as they were.
    after = factory.branches()
    assert {name: after[name] for name in before} == before
    assert all(name.startswith(f"factory/{run_id}") for name in after.keys() - before.keys())
    assert factory.git("status", "--porcelain") == ""

    # The agent gets an immutable brief of pointers to files rendered from the snapshot.
    assert (run_dir / "spec.md").read_text() == "# Greeting\n\nSay hello.\n"
    assert (run_dir / "tickets" / "t1" / "ticket.md").read_text() == "# Greet\n\nWrite hello.txt.\n"
    assert first.brief.is_relative_to(run_dir)
    assert not os.access(first.brief, os.W_OK)
    brief = json.loads(first.brief.read_text())
    assert brief["inputs"] == [
        {"source": "spec", "path": str(run_dir / "spec.md")},
        {"source": "ticket", "path": str(run_dir / "tickets" / "t1" / "ticket.md")},
    ]
    assert brief["base"] == base
    output = Path(brief["output_dir"]) / "output.json"
    assert json.loads(output.read_text()) == {
        "summary": "Add hello.txt\n\nFactory-Ticket: forged",
        "handoff": "hello.txt exists",
    }

    # The Run prints its id first, then the tree as it moves, and its summary at the end.
    lines = result.out.splitlines()
    assert lines[0] == f"Run {run_id} started on factory/{run_id}"
    for node in ("implement", "merge"):
        live = rf"t1 +{node} +active +implement +1 +1 +\d+s +\$0\.00"
        assert any(re.fullmatch(live, line) for line in lines)
    took = factory.run_took(run_id)
    assert f"Run {run_id}  [done]  factory/{run_id}  {took}  $0.00" in lines
    t1, t2 = factory.ticket_took(run_id, "t1"), factory.ticket_took(run_id, "t2")
    index = max(i for i, line in enumerate(lines) if line.startswith("Ticket "))
    rule = "------  ----  --------  ---------  -----  -------  --------  -----"
    assert lines[index : index + 6] == [
        "Ticket  Node  State     Stage      Round  Attempt  Duration  Cost",
        rule,
        f"t1      done  merged    implement  1      1        {t1:8}  $0.00",
        f"t2      done  merged    implement  1      1        {t2:8}  $0.00",
        rule,
        f"TOTAL   done  2 merged{'':29}{took:8}  $0.00",
    ]
    assert f"Run {run_id} finished: done in {took}, $0.00" in lines

    # Status shows each Ticket with its node, Round, Attempt and the Run's state.
    status = factory.cli("status", run_id)
    assert status.code == 0
    assert status.out.splitlines() == [
        f"Run {run_id}  done  factory/{run_id}  {took}  $0.00",
        "TICKET  NODE  LAST STAGE  ROUND  ATTEMPT  LOST  STATE   DURATION  COST",
        f"t1      done  implement   1      1        0     merged  {t1:8}  $0.00",
        f"t2      done  implement   1      1        0     merged  {t2:8}  $0.00",
    ]


def test_run_refuses_a_pipeline_that_routes_to_an_undeclared_node(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.pipeline["stages"]["implement"]["next"] = {"done": "build"}
    before = factory.branches()

    result = factory.run(spec)

    assert result.code == 2
    assert factory.branches() == before


def test_status_of_an_unknown_run_fails(factory: Harness) -> None:
    assert factory.cli("status", "nope").code == 2


def test_ticket_with_broken_front_matter_stops_the_run_before_it_starts(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": "---\ntitle: [Greet\n---\nWrite hello.txt.\n"})
    before = factory.branches()

    result = factory.run(spec)

    assert result.code == 2
    assert result.err.startswith(f"factory: {spec / 'tickets' / 't1.md'}: front matter is not valid YAML")
    assert factory.branches() == before
