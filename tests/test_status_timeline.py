import re

from harness import AgentScript, Harness, run_id_of, ticket_text

RESOLVED = AgentScript(
    files={"note.txt": "a and b\n"}, output={"summary": "Keep both", "handoff": "kept"}, turns=5, cost=0.08
)
FIXED = AgentScript(files={"fixed.txt": "fixed\n"}, output={"summary": "Fix", "handoff": "fixed"}, turns=13, cost=0.14)


def conflicting_run(factory: Harness) -> str:
    """Runs two Tickets that conflict in `note.txt` through `sync`, a Gate `check` that fails once, and `fix`."""
    factory.project["max_parallel"] = 2
    factory.pipeline["roles"]["reconciler"] = factory.pipeline["roles"]["implementer"]
    factory.pipeline["stages"]["resolve"] = {
        "role": "reconciler",
        "inputs": ["spec", "ticket", "cause"],
        "next": {"done": "sync"},
    }
    factory.pipeline["stages"]["fix"] = {"role": "implementer", "inputs": ["ticket", "cause"], "next": {"done": "sync"}}
    factory.pipeline["stages"]["implement"]["next"]["done"] = "sync"
    factory.pipeline["operations"]["sync"]["next"] = {"clean": "check", "conflict": "resolve"}
    factory.pipeline["gates"] = {
        "check": {"kind": "command", "with": {"run": "test -f fixed.txt"}, "next": {"pass": "merge", "fail": "fix"}}
    }
    spec = factory.write_spec("Notes", "Two notes.", {name: ticket_text(name) for name in "ab"})
    for name in "ab":
        factory.executor.ticket_scripts[(name, "implement")] = AgentScript(
            files={"note.txt": f"{name}\n"}, output={"summary": f"Note {name}", "handoff": "done"}
        )
    factory.executor.scripts["resolve"] = RESOLVED
    factory.executor.scripts["fix"] = FIXED
    result = factory.run(spec)
    assert result.code == 0, result.out
    return run_id_of(result.out)


def test_timeline_lists_every_node_entry_with_its_outcome_and_the_cost_of_the_run(factory: Harness) -> None:
    run_id = conflicting_run(factory)

    result = factory.cli("status", run_id, "--timeline")

    assert result.code == 0, result.out
    rows = [re.sub(r"(\d+ min )?\d+ s", "N s", " ".join(line.split()[1:])) for line in result.out.splitlines()]
    assert rows[0].startswith("- run started ")
    loser = next(row.split()[0] for row in rows if row.endswith(" sync conflict note.txt"))
    assert f"{loser} check fail probe 1 exit code 1" in rows
    assert f"{loser} resolve done N s 5 turns $0.08" in rows
    winner = "a" if loser == "b" else "b"
    assert f"{winner} fix done N s 13 turns $0.14" in rows
    assert f"{winner} check pass probe 2" in rows
    assert f"{winner} merge merged" in rows
    assert rows[-1] == "- run done N s $0.36"


def test_status_without_timeline_keeps_the_ticket_table(factory: Harness) -> None:
    run_id = conflicting_run(factory)

    result = factory.cli("status", run_id)

    assert result.out.splitlines()[1].split() == [
        "TICKET",
        "NODE",
        "LAST",
        "STAGE",
        "ROUND",
        "ATTEMPT",
        "LOST",
        "STATE",
    ]
