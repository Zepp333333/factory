import json
from pathlib import Path

import pytest

from factory.core.git import GitError, HostGit, MergeConflict
from harness import AgentScript, Harness, git, run_id_of, ticket_text, writes

RENAME = AgentScript(
    run=['for f in *.txt; do mv "$f" "${f%.txt}.md"; done'], output={"summary": "Rename", "handoff": "renamed"}
)


WINDOW = {"MergeWindowQueued": "queued", "MergeWindowGranted": "granted", "MergeWindowReleased": "released"}


def catch_up_then_gate(factory: Harness, run: str) -> None:
    """Routes `implement` through `catch_up` and a Gate `check` on the combined tree to `merge`."""
    factory.pipeline["stages"]["implement"]["next"]["done"] = "catch_up"
    factory.pipeline["operations"]["catch_up"]["next"]["clean"] = "check"
    factory.pipeline["gates"] = {
        "check": {"kind": "command", "with": {"run": run}, "next": {"pass": "merge", "fail": "fix"}}
    }
    factory.pipeline["stages"]["fix"] = {
        "role": "implementer",
        "inputs": ["ticket", "cause"],
        "next": {"done": "catch_up"},
    }


def window_steps(factory: Harness, run_id: str) -> list[str]:
    """Lists what happened to the Merge window in order, such as `queued a`, `granted a` and `released a`."""
    return [f"{WINDOW[type]} {event['ticket']}" for type, event in factory.all_events(run_id) if type in WINDOW]


def test_merge_window_goes_to_one_ticket_at_a_time_in_the_order_they_ask_and_merge_only_fast_forwards(
    factory: Harness,
) -> None:
    factory.project["max_parallel"] = 3
    everyone_queued = factory.root / "everyone-queued"
    catch_up_then_gate(factory, f"until [ -e {everyone_queued} ]; do sleep 0.01; done")
    spec = factory.write_spec("Files", "Three files.", {name: ticket_text(name) for name in "abc"})
    for name in "abc":
        factory.executor.ticket_scripts[(name, "implement")] = writes(name)

    def open_the_gate_once_everyone_queued(seconds: float) -> None:
        if sum(type == "MergeWindowQueued" for _, type, _ in factory.all_runs_events()) == 3:
            everyone_queued.touch()

    factory.clock.on_sleep = open_the_gate_once_everyone_queued

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    steps = window_steps(factory, run_id)
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
    # Each catch_up brings the Integration branch's tip into its Ticket, so each merge only fast-forwards to it.
    events = factory.events(run_id, "OperationFinished")
    catch_ups = [e for e in events if e["kind"] == "catch_up"]
    merges = [e for e in events if e["kind"] == "merge"]
    tips = [factory.git("rev-parse", "main"), *(e["after"] for e in merges)]
    assert [e["outcome"] for e in catch_ups] == ["clean"] * 3
    assert [e["before"] for e in catch_ups] == [e["before"] for e in merges] == tips[:-1]
    assert [e["after"] for e in merges] == [e["after"] for e in catch_ups]
    integration = f"factory/{run_id}"
    assert factory.git("rev-parse", integration) == tips[-1]
    assert factory.git("rev-list", "--merges", "--count", integration) == "2"


def test_semantic_conflict_fails_the_gate_after_catch_up_and_the_ticket_gives_up_the_window_for_the_fix_loop(
    factory: Harness,
) -> None:
    """Runs two Tickets whose files pass the Gate alone but not together.

    The Gate allows one `.txt` file, so the Ticket that merges second fails it after `catch_up` and renames files in
    `fix`.
    """
    catch_up_then_gate(factory, "test $(ls *.txt | wc -l) -le 1")
    factory.project["max_parallel"] = 2
    spec = factory.write_spec("Files", "Two files.", {name: ticket_text(name) for name in "ab"})
    for name in "ab":
        factory.executor.ticket_scripts[(name, "implement")] = writes(name)
    factory.executor.scripts["fix"] = RENAME

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    held = [step for step in window_steps(factory, run_id) if not step.startswith("queued")]
    first, late = held[0].split()[1], held[2].split()[1]
    assert held == [f"granted {first}", f"released {first}"] + [f"granted {late}", f"released {late}"] * 2
    assert late != first
    outcomes = [(e["ticket"], e["outcome"]) for e in factory.events(run_id, "GateFinished")]
    assert outcomes == [(first, "pass"), (late, "fail"), (late, "pass")]
    assert [r.stage for r in factory.executor.requests if r.stage == "fix"] == ["fix"]
    files = factory.git("ls-tree", "--name-only", f"factory/{run_id}").splitlines()
    assert sorted(f for f in files if f.endswith((".txt", ".md")) and f != "README.md") == ["a.md", "b.md"]


def test_catch_up_that_git_fails_is_an_error_and_not_a_conflict(factory: Harness) -> None:
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


def conflicting_notes(factory: Harness, resolve: AgentScript | list[AgentScript], attempts: int = 1) -> Path:
    """Writes a spec of two Tickets that write `note.txt` differently, with a `resolve` Stage on the conflict."""
    factory.project["max_parallel"] = 2
    factory.pipeline["roles"]["reconciler"] = factory.pipeline["roles"]["implementer"]
    factory.pipeline["stages"]["resolve"] = {
        "role": "reconciler",
        "inputs": ["spec", "ticket", "cause"],
        "attempts": attempts,
        "next": {"done": "catch_up"},
    }
    factory.pipeline["operations"]["catch_up"]["next"]["conflict"] = "resolve"
    spec = factory.write_spec("Notes", "Two notes.", {name: ticket_text(name) for name in "ab"})
    for name in "ab":
        factory.executor.ticket_scripts[(name, "implement")] = AgentScript(
            files={"note.txt": f"{name}\n"}, output={"summary": f"Note {name}", "handoff": "done"}
        )
    factory.executor.scripts["resolve"] = resolve
    return spec


RESOLVED = AgentScript(files={"note.txt": "a and b\n"}, output={"summary": "Keep both notes", "handoff": "kept both"})
MARKERS = AgentScript(output={"summary": "Leave it", "handoff": "did nothing"})


def test_catch_up_conflict_lists_files_and_merged_tickets_and_the_host_commits_the_merge_once_no_markers_are_left(
    factory: Harness,
) -> None:
    result = factory.run(conflicting_notes(factory, [MARKERS, RESOLVED], attempts=2))

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [merged, _] = [e for e in factory.events(run_id, "OperationFinished") if e["kind"] == "merge"]
    [failed, done] = [e for e in factory.events(run_id, "AttemptFinished") if e["node"] == "resolve"]
    assert (failed["outcome"], failed["cause"]) == ("failed", "conflict_markers")
    assert failed["errors"] == ["note.txt still holds conflict markers"]
    assert done["outcome"] == "done"
    assert "<<<<<<<" in Path(failed["discarded"]).read_text()
    resolve = [r for r in factory.executor.requests if r.stage == "resolve"][-1]
    brief = json.loads(resolve.brief.read_text())
    [catch_up] = [i for i in brief["inputs"] if i["source"] == "cause"]
    assert (catch_up["kind"], catch_up["node"]) == ("operation", "catch_up")
    artifact = json.loads(Path(catch_up["path"]).read_text())
    assert artifact["files"] == ["note.txt"]
    [landed] = artifact["merged_since_base"]
    assert landed == {
        "ticket": merged["ticket"],
        "before": merged["before"],
        "after": merged["after"],
        "ticket_file": str(factory.home / "runs" / run_id / "tickets" / merged["ticket"] / "ticket.md"),
    }
    assert Path(landed["ticket_file"]).read_text().startswith(f"# {merged['ticket']}\n")
    assert artifact["tip"] == merged["after"]
    integration = f"factory/{run_id}"
    assert factory.git("show", f"{integration}:note.txt") == "a and b"
    [commit] = [
        c
        for c in factory.git("rev-list", "--merges", integration).splitlines()
        if "Factory-Stage: resolve" in factory.git("log", "-1", "--format=%B", c)
    ]
    assert factory.git("rev-parse", f"{commit}^2") == artifact["tip"]
    assert factory.git("log", "-1", "--format=%s", commit) == "Keep both notes"
    assert [e["outcome"] for e in factory.events(run_id, "OperationFinished") if e["kind"] == "catch_up"] == [
        "clean",
        "conflict",
        "clean",
    ]


def test_text_conflict_in_catch_up_leaves_the_ticket_branch_and_markers_that_never_go_abandon_it_without_a_merge(
    factory: Harness,
) -> None:
    result = factory.run(conflicting_notes(factory, MARKERS, attempts=2))

    assert result.code == 1, result.out
    run_id = run_id_of(result.out)
    catch_ups = [
        (e["outcome"], e["before"], e["after"])
        for e in factory.events(run_id, "OperationFinished")
        if e["kind"] == "catch_up"
    ]
    [(first, _, _), (second, tip, kept)] = catch_ups
    assert (first, second) == ("clean", "conflict")
    [merged] = [e for e in factory.events(run_id, "OperationFinished") if e["kind"] == "merge"]
    assert tip == merged["after"]
    assert factory.git("show", f"factory/{run_id}:note.txt") == merged["ticket"]
    late = "b" if merged["ticket"] == "a" else "a"
    assert factory.git("rev-parse", f"refs/factory/{run_id}/abandoned/{late}") == kept
    assert factory.git("rev-list", "--merges", "--count", f"factory/{run_id}") == "0"
