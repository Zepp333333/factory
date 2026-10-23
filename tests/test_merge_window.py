import yaml

from harness import AgentScript, Harness, run_id_of

RENAME = AgentScript(
    run=['for f in *.txt; do mv "$f" "${f%.txt}.md"; done'], output={"summary": "Rename", "handoff": "renamed"}
)


def ticket(name: str) -> str:
    return f"---\n{yaml.safe_dump({'title': name})}---\nWrite {name}.txt.\n"


def writes(name: str, delay: float = 0) -> AgentScript:
    run = [f"sleep {delay}"] if delay else []
    return AgentScript(
        files={f"{name}.txt": f"{name}\n"}, run=run, output={"summary": f"Add {name}", "handoff": "done"}
    )


WINDOW = ("MergeWindowGranted", "MergeWindowReleased")


def sync_then_gate(factory: Harness, run: str) -> None:
    """Routes `implement` through `sync` and a Gate `check` on the combined tree to `merge`."""
    factory.pipeline["stages"]["implement"]["next"]["done"] = "sync"
    factory.pipeline["operations"]["sync"]["next"]["clean"] = "check"
    factory.pipeline["gates"] = {
        "check": {"kind": "command", "with": {"run": run}, "next": {"pass": "merge", "fail": "fix"}}
    }
    factory.pipeline["stages"]["fix"] = {
        "role": "implementer",
        "inputs": ["ticket", "cause"],
        "next": {"done": "sync"},
    }


def test_merge_window_goes_to_one_ticket_at_a_time_in_the_order_they_ask(factory: Harness) -> None:
    factory.project["max_parallel"] = 3
    sync_then_gate(factory, "sleep 0.4")
    spec = factory.write_spec("Files", "Three files.", {name: ticket(name) for name in ("a", "b", "c")})
    for name, delay in (("a", 0), ("b", 0.2), ("c", 0.4)):
        factory.executor.ticket_scripts[(name, "implement")] = writes(name, delay)

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    marks = {"MergeWindowQueued": "queued", "MergeWindowGranted": "granted", "MergeWindowReleased": "released"}
    steps = [f"{marks[t]} {e['ticket']}" for t, e in factory.all_events(run_id) if t in marks]
    queued = [s.split()[1] for s in steps if s.startswith("queued")]
    held = [s for s in steps if not s.startswith("queued")]
    assert held == [f"{verb} {name}" for name in queued for verb in ("granted", "released")]


def test_ticket_that_returns_to_a_stage_gives_up_the_window_and_queues_again(factory: Harness) -> None:
    sync_then_gate(factory, "test $(ls *.txt | wc -l) -le 1")
    factory.project["max_parallel"] = 2
    spec = factory.write_spec("Files", "Two files.", {name: ticket(name) for name in ("a", "b")})
    factory.executor.ticket_scripts[("a", "implement")] = writes("a")
    factory.executor.ticket_scripts[("b", "implement")] = writes("b", 0.8)
    factory.executor.scripts["fix"] = RENAME

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    held = [(t, e["ticket"]) for t, e in factory.all_events(run_id) if t in WINDOW]
    assert [t for t, _ in held] == ["MergeWindowGranted", "MergeWindowReleased"] * 3
    first, late = held[0][1], held[2][1]
    assert [ticket for _, ticket in held] == [first] * 2 + [late] * 4


def test_semantic_conflict_fails_the_gate_after_sync_and_goes_through_the_fix_loop(factory: Harness) -> None:
    sync_then_gate(factory, "test $(ls *.txt | wc -l) -le 1")
    factory.project["max_parallel"] = 2
    spec = factory.write_spec("Files", "Two files.", {name: ticket(name) for name in ("a", "b")})
    factory.executor.ticket_scripts[("a", "implement")] = writes("a")
    factory.executor.ticket_scripts[("b", "implement")] = writes("b", 0.8)
    factory.executor.scripts["fix"] = RENAME

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    outcomes = [(e["ticket"], e["outcome"]) for e in factory.events(run_id, "GateFinished")]
    [(_, first), (late, second), (also_late, third)] = outcomes
    assert (first, second, third) == ("pass", "fail", "pass")
    assert late == also_late
    assert [r.stage for r in factory.executor.requests if r.stage == "fix"] == ["fix"]
    integration = f"factory/{run_id}"
    files = factory.git("ls-tree", "--name-only", integration).splitlines()
    assert sorted(f for f in files if f.endswith((".txt", ".md")) and f != "README.md") == ["a.md", "b.md"]


def test_sync_brings_the_integration_tip_into_the_ticket_and_merge_only_fast_forwards(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    spec = factory.write_spec("Files", "Two files.", {name: ticket(name) for name in ("a", "b")})
    factory.executor.ticket_scripts[("a", "implement")] = writes("a")
    factory.executor.ticket_scripts[("b", "implement")] = writes("b", 0.8)

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    events = factory.events(run_id, "OperationFinished")
    syncs = [e for e in events if e["kind"] == "sync"]
    [first, last] = [e for e in events if e["kind"] == "merge"]
    assert [e["outcome"] for e in syncs] == ["clean", "clean"]
    assert last["before"] == first["after"]
    assert [e["before"] for e in syncs if e["ticket"] == last["ticket"]] == [first["after"]]
    integration = f"factory/{run_id}"
    assert factory.git("rev-parse", integration) == last["after"]
    assert factory.git("rev-list", "--merges", "--count", integration) == "1"
