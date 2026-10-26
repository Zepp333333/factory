import pytest

from factory.git import GitError, HostGit, MergeConflict
from harness import AgentScript, Harness, git, run_id_of, ticket_text, writes

RENAME = AgentScript(
    run=['for f in *.txt; do mv "$f" "${f%.txt}.md"; done'], output={"summary": "Rename", "handoff": "renamed"}
)


WINDOW = {"MergeWindowQueued": "queued", "MergeWindowGranted": "granted", "MergeWindowReleased": "released"}


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


def window_steps(factory: Harness, run_id: str) -> list[str]:
    """Lists what happened to the Merge window in order, such as `queued a`, `granted a` and `released a`."""
    return [f"{WINDOW[type]} {event['ticket']}" for type, event in factory.all_events(run_id) if type in WINDOW]


def test_merge_window_goes_to_one_ticket_at_a_time_in_the_order_they_ask(factory: Harness) -> None:
    factory.project["max_parallel"] = 3
    everyone_queued = factory.root / "everyone-queued"
    sync_then_gate(factory, f"until [ -e {everyone_queued} ]; do sleep 0.01; done")
    spec = factory.write_spec("Files", "Three files.", {name: ticket_text(name) for name in "abc"})
    for name in "abc":
        factory.executor.ticket_scripts[(name, "implement")] = writes(name)

    def open_the_gate_once_everyone_queued(seconds: float) -> None:
        if sum(type == "MergeWindowQueued" for _, type, _ in factory.all_runs_events()) == 3:
            everyone_queued.touch()

    factory.clock.on_sleep = open_the_gate_once_everyone_queued

    result = factory.run(spec)

    assert result.code == 0, result.out
    steps = window_steps(factory, run_id_of(result.out))
    first, second, third = [step.split()[1] for step in steps if step.startswith("queued")]
    assert steps == [
        f"queued {first}",
        f"granted {first}",
        f"queued {second}",
        f"queued {third}",
        f"released {first}",
        f"granted {second}",
        f"released {second}",
        f"granted {third}",
        f"released {third}",
    ]


def run_two_tickets_that_conflict_in_meaning(factory: Harness) -> str:
    """Runs two Tickets whose files pass the Gate alone but not together, and returns the Run's id.

    The Gate allows one `.txt` file, so the Ticket that merges second fails it after `sync` and renames files in `fix`.
    """
    sync_then_gate(factory, "test $(ls *.txt | wc -l) -le 1")
    factory.project["max_parallel"] = 2
    spec = factory.write_spec("Files", "Two files.", {name: ticket_text(name) for name in "ab"})
    for name in "ab":
        factory.executor.ticket_scripts[(name, "implement")] = writes(name)
    factory.executor.scripts["fix"] = RENAME

    result = factory.run(spec)

    assert result.code == 0, result.out
    return run_id_of(result.out)


def test_ticket_that_returns_to_a_stage_gives_up_the_window_and_queues_again(factory: Harness) -> None:
    run_id = run_two_tickets_that_conflict_in_meaning(factory)

    held = [step for step in window_steps(factory, run_id) if not step.startswith("queued")]
    first, late = held[0].split()[1], held[2].split()[1]
    assert held == [f"granted {first}", f"released {first}"] + [f"granted {late}", f"released {late}"] * 2
    assert late != first


def test_semantic_conflict_fails_the_gate_after_sync_and_goes_through_the_fix_loop(factory: Harness) -> None:
    run_id = run_two_tickets_that_conflict_in_meaning(factory)

    outcomes = [(e["ticket"], e["outcome"]) for e in factory.events(run_id, "GateFinished")]
    [(first, passed), (late, failed), (also_late, passed_after_fix)] = outcomes
    assert (passed, failed, passed_after_fix) == ("pass", "fail", "pass")
    assert late == also_late != first
    assert [r.stage for r in factory.executor.requests if r.stage == "fix"] == ["fix"]
    files = factory.git("ls-tree", "--name-only", f"factory/{run_id}").splitlines()
    assert sorted(f for f in files if f.endswith((".txt", ".md")) and f != "README.md") == ["a.md", "b.md"]


def test_sync_brings_the_integration_tip_into_the_ticket_and_merge_only_fast_forwards(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    spec = factory.write_spec("Files", "Two files.", {name: ticket_text(name) for name in "ab"})
    for name in "ab":
        factory.executor.ticket_scripts[(name, "implement")] = writes(name)

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


def test_text_conflict_in_sync_leaves_the_ticket_branch_and_takes_the_conflict_route(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    spec = factory.write_spec("Notes", "Two notes.", {name: ticket_text(name) for name in "ab"})
    for name in ("a", "b"):
        factory.executor.ticket_scripts[(name, "implement")] = AgentScript(
            files={"note.txt": f"{name}\n"}, output={"summary": f"Note {name}", "handoff": "done"}
        )

    result = factory.run(spec)

    assert result.code == 1, result.out
    run_id = run_id_of(result.out)
    syncs = [
        (e["outcome"], e["before"], e["after"])
        for e in factory.events(run_id, "OperationFinished")
        if e["kind"] == "sync"
    ]
    [(first, _, _), (second, tip, kept)] = syncs
    assert (first, second) == ("clean", "conflict")
    [merged] = [e for e in factory.events(run_id, "OperationFinished") if e["kind"] == "merge"]
    assert tip == merged["after"]
    assert factory.git("show", f"factory/{run_id}:note.txt") == merged["ticket"]
    late = "b" if merged["ticket"] == "a" else "a"
    assert factory.git("rev-parse", f"refs/factory/{run_id}/abandoned/{late}") == kept


def test_sync_that_git_fails_is_an_error_and_not_a_conflict(factory: Harness) -> None:
    factory.git("branch", "factory/run--a", "main")
    workspace = factory.root / "workspace"
    factory.git("worktree", "add", "-q", str(workspace), "factory/run--a")
    (workspace / "a.txt").write_text("a\n")
    git(workspace, "add", "a.txt")
    git(workspace, "commit", "-q", "-m", "a")
    (factory.repo / "b.txt").write_text("b\n")
    factory.git("add", "b.txt")
    factory.git("commit", "-q", "-m", "b")
    tree = factory.git("rev-parse", "main^{tree}")
    (factory.repo / ".git" / "objects" / tree[:2] / tree[2:]).unlink()

    with pytest.raises(GitError) as raised:
        HostGit(factory.repo).catch_up(workspace, "factory/run--a", factory.git("rev-parse", "main"))

    assert not isinstance(raised.value, MergeConflict)
