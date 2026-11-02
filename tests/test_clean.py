from pathlib import Path

from harness import AgentScript, Harness, git, run_id_of
from test_human_decider import asking
from test_recovery import STALE, crashed_run, killed_runs_run_no_handler  # noqa: F401

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
AFTER = "---\ntitle: Greet again\nblocked_by: [t1]\n---\nWrite bad.txt.\n"
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "wrote hello.txt"})
BAD = AgentScript(files={"bad.txt": "bad\n"}, output={"summary": "Add bad.txt", "handoff": "wrote bad.txt"})
STEPS = ["SandboxRemoved", "WorkspaceRemoved", "TicketBranchDeleted", "CleanupFailed", "AbandonedBranchSaved"]


def worktrees(factory: Harness) -> list[Path]:
    lines = factory.git("worktree", "list", "--porcelain").splitlines()
    return [Path(line.removeprefix("worktree ")).resolve() for line in lines if line.startswith("worktree ")]


def steps(factory: Harness, run_id: str, ticket: str = "t1") -> list[str]:
    return [type for type, payload in factory.all_events(run_id) if type in STEPS and payload["ticket"] == ticket]


def merged_and_abandoned(factory: Harness) -> Path:
    """Sets up t1, which merges, and t2 after it, which commits bad.txt and is abandoned by the Gate `build`."""
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {
        "build": {
            "kind": "command",
            "with": {"run": "test ! -f bad.txt"},
            "next": {"pass": "merge", "fail": "abandoned"},
        }
    }
    factory.executor.ticket_scripts[("t1", "implement")] = HELLO
    factory.executor.ticket_scripts[("t2", "implement")] = BAD
    return factory.write_spec("Greeting", "Say hello.", {"t1": TICKET, "t2": AFTER})


def test_sandbox_that_does_not_stop_keeps_what_it_works_on_until_clean_removes_it(factory: Harness) -> None:
    spec = merged_and_abandoned(factory)
    factory.sandbox.refuse_remove = True

    result = factory.run(spec)

    assert result.code == 1, result.out
    run_id = run_id_of(result.out)
    # The cleanup of t1 fails at its Sandbox, keeps its Workspace and branch, and does not stop t2 or its outcome.
    assert factory.events(run_id, "CleanupFailed") == [
        {"ticket": t, "step": "sandbox", "reason": f"Sandbox {run_id}-{t} did not stop"} for t in ("t1", "t2")
    ]
    status = factory.cli("status", run_id).out.splitlines()
    assert [row.split()[6] for row in status[2:4]] == ["merged", "abandoned"]
    for ticket in ("t1", "t2"):
        assert factory.workspace(run_id, ticket).resolve() in worktrees(factory)
        assert f"factory/{run_id}--{ticket}" in factory.branches()
    assert f"refs/factory/{run_id}/abandoned/t2" in factory.git("for-each-ref", "refs/factory")

    # Without a flag `clean` prints the leftovers and changes nothing.
    branches, trees, events = factory.branches(), worktrees(factory), factory.all_events(run_id)
    plan = factory.cli("clean")

    assert plan.code == 0, plan.err
    assert f"Run {run_id} (partial), Ticket t1:" in plan.out
    assert f"remove Sandbox {run_id}-t1" in plan.out
    assert f"remove Workspace {factory.workspace(run_id, 't1')}" in plan.out
    assert f"remove branch factory/{run_id}--t1, its commits are in factory/{run_id}" in plan.out
    assert "factory clean --yes" in plan.out
    assert (factory.branches(), worktrees(factory), factory.all_events(run_id)) == (branches, trees, events)
    assert factory.workspace(run_id, "t1").is_dir()

    # A Sandbox that still does not stop keeps the Workspace and branch.
    kept = factory.cli("clean", "--yes")

    assert kept.code == 0, kept.err
    assert f"kept Sandbox {run_id}-t1: Sandbox {run_id}-t1 did not stop" in kept.out
    assert factory.workspace(run_id, "t1").resolve() in worktrees(factory)
    assert f"factory/{run_id}--t1" in factory.branches()

    # Once the Sandbox stops, `--yes` removes everything and journals each step to the Run.
    factory.sandbox.refuse_remove = False
    removed = factory.cli("clean", "--yes")

    assert removed.code == 0, removed.err
    for ticket in ("t1", "t2"):
        assert not factory.workspace(run_id, ticket).exists()
        assert f"factory/{run_id}--{ticket}" not in factory.branches()
    assert steps(factory, run_id, "t1")[-3:] == ["SandboxRemoved", "WorkspaceRemoved", "TicketBranchDeleted"]
    assert steps(factory, run_id, "t2")[-4:] == [
        "SandboxRemoved",
        "WorkspaceRemoved",
        "AbandonedBranchSaved",
        "TicketBranchDeleted",
    ]
    assert factory.git("worktree", "list", "--porcelain").count("worktree ") == 1
    assert factory.events(run_id, "SandboxRemoved") == [{"ticket": t, "sandbox": f"{run_id}-{t}"} for t in ("t1", "t2")]
    assert factory.cli("clean").out == "Nothing to clean\n"


def test_workspace_with_uncommitted_edits_stays_with_its_branch_through_the_cleanup_and_clean(
    factory: Harness,
) -> None:
    spec = merged_and_abandoned(factory)
    factory.workbench.late_files = {"late.txt": "written after the outcome\n"}

    result = factory.run(spec)

    assert result.code == 1, result.out
    run_id = run_id_of(result.out)
    # The cleanup after the outcome keeps each Workspace with uncommitted edits and its branch, and the outcome holds.
    for ticket in ("t1", "t2"):
        workspace = factory.workspace(run_id, ticket)
        assert (workspace / "late.txt").read_text() == "written after the outcome\n"
        assert workspace.resolve() in worktrees(factory)
        assert f"factory/{run_id}--{ticket}" in factory.branches()
    assert steps(factory, run_id, "t1") == ["SandboxRemoved", "CleanupFailed"]
    assert steps(factory, run_id, "t2") == ["AbandonedBranchSaved", "SandboxRemoved", "CleanupFailed"]
    for failed in factory.events(run_id, "CleanupFailed"):
        assert failed["step"] == "workspace"
        assert "modified or untracked files" in failed["reason"]
    assert factory.git("rev-parse", f"refs/factory/{run_id}/abandoned/t2") == factory.git(
        "rev-parse", f"factory/{run_id}--t2"
    )
    status = factory.cli("status", run_id).out.splitlines()
    assert status[0].startswith(f"Run {run_id}  partial  factory/{run_id}")
    assert [row.split()[6] for row in status[2:4]] == ["merged", "abandoned"]

    # Without a flag `clean` plans to keep the Workspace and its branch and changes nothing.
    workspace = factory.workspace(run_id, "t1")
    branches, trees, events = factory.branches(), worktrees(factory), factory.all_events(run_id)
    plan = factory.cli("clean")

    assert plan.code == 0, plan.err
    assert f"keep Workspace {workspace} and its branch, it has uncommitted changes: late.txt" in plan.out
    assert f"remove Workspace {workspace}" not in plan.out
    assert f"remove branch factory/{run_id}--t1" not in plan.out
    assert (factory.branches(), worktrees(factory), factory.all_events(run_id)) == (branches, trees, events)

    # With `--yes` the Workspace stays and the output names the reason.
    kept = factory.cli("clean", "--yes")

    assert kept.code == 0, kept.err
    assert f"kept Workspace {workspace}" in kept.out
    assert "modified or untracked files" in kept.out
    assert (workspace / "late.txt").is_file()
    assert workspace.resolve() in worktrees(factory)
    assert f"factory/{run_id}--t1" in factory.branches()
    failed = [e for e in factory.events(run_id, "CleanupFailed") if e["ticket"] == "t1"][-1]
    assert failed["step"] == "workspace"


def test_clean_waits_until_a_run_stops_then_saves_unsaved_work_and_removes_only_the_factorys_leftovers(
    factory: Harness,
) -> None:
    factory.project["sandbox"] = {"kind": "sbx"}
    asking(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    run_id = crashed_run(factory, spec, lambda run_id: bool(factory.waiting()))

    # A Run that still runs is not touched.
    branches, trees = factory.branches(), worktrees(factory)
    running = factory.cli("clean", "--yes")

    assert running.out == "Nothing to clean\n"
    assert (factory.branches(), worktrees(factory)) == (branches, trees)
    assert factory.workspace(run_id, "t1").is_dir()

    # Once its heartbeat is stale, the plan names the ref that the unsaved work goes to and changes nothing.
    factory.clock.sleep(STALE.total_seconds())
    plan = factory.cli("clean")

    assert f"Run {run_id} (orphaned), Ticket t1:" in plan.out
    assert f"goes to refs/factory/{run_id}/abandoned/t1 first" in plan.out
    assert f"factory/{run_id}--t1" in factory.branches()
    assert factory.git("for-each-ref", "refs/factory") == ""

    # `--yes` saves the branch under its ref before it deletes it, and leaves what is not the factory's.
    foreign = factory.root / "foreign"
    factory.git("worktree", "add", "-q", "-b", "owner/work", str(foreign))
    factory.git("branch", f"factory/{run_id}-owner", "main")
    before = {k: v for k, v in factory.branches().items() if k != f"factory/{run_id}--t1"}
    head = factory.git("rev-parse", f"factory/{run_id}--t1")
    result = factory.cli("clean", "--yes")

    assert result.code == 0, result.err
    ref = f"refs/factory/{run_id}/abandoned/t1"
    assert factory.git("rev-parse", ref) == head
    assert factory.git("show", f"{ref}:hello.txt") == "hello"
    assert f"factory/{run_id}--t1" not in factory.branches()
    assert not factory.workspace(run_id, "t1").exists()
    assert factory.events(run_id, "AbandonedBranchSaved") == [{"ticket": "t1", "ref": ref, "commit": head}]
    assert steps(factory, run_id)[-4:] == [
        "SandboxRemoved",
        "WorkspaceRemoved",
        "AbandonedBranchSaved",
        "TicketBranchDeleted",
    ]
    assert f"saved factory/{run_id}--t1 under {ref}" in result.out
    assert f"removed Sandbox {run_id}-t1" in result.out
    assert [call[-1] for call in factory.sbx.calls("rm")] == [f"{run_id}-t1"]
    assert factory.branches() == before
    assert foreign.resolve() in worktrees(factory)
    assert git(foreign, "rev-parse", "--abbrev-ref", "HEAD") == "owner/work"
