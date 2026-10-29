from pathlib import Path

import pytest

from factory.sandbox import SandboxError
from harness import AgentScript, Harness, git, run_id_of
from test_human_decider import asking
from test_recovery import STALE, crashed_run, killed_runs_run_no_handler  # noqa: F401

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "wrote hello.txt"})
STEPS = ["SandboxRemoved", "WorkspaceRemoved", "TicketBranchDeleted", "CleanupFailed", "AbandonedBranchSaved"]


def worktrees(factory: Harness) -> list[Path]:
    lines = factory.git("worktree", "list", "--porcelain").splitlines()
    return [Path(line.removeprefix("worktree ")).resolve() for line in lines if line.startswith("worktree ")]


def steps(factory: Harness, run_id: str) -> list[str]:
    return [type for type, _ in factory.all_events(run_id) if type in STEPS]


def merged_run_with_leftovers(factory: Harness) -> str:
    """Runs a Ticket to `merged` whose Sandbox, Workspace and branch the cleanup after it could not remove."""
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO
    factory.sandbox.refuse_remove = True
    result = factory.run(spec)
    assert result.code == 0, result.out
    factory.sandbox.refuse_remove = False
    return run_id_of(result.out)


def crashed_waiting_run(factory: Harness) -> str:
    asking(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    return crashed_run(factory, spec, lambda run_id: bool(factory.waiting()))


def crashed_with_work(factory: Harness) -> str:
    """Crashes the Run's process while its Ticket, which committed hello.txt, waits for a human."""
    run_id = crashed_waiting_run(factory)
    factory.clock.sleep(STALE.total_seconds())
    return run_id


def test_without_a_flag_it_prints_the_leftovers_and_changes_nothing(factory: Harness) -> None:
    run_id = merged_run_with_leftovers(factory)
    branches, trees, events = factory.branches(), worktrees(factory), factory.all_events(run_id)

    result = factory.cli("clean")

    assert result.code == 0, result.err
    assert f"Run {run_id} (done), Ticket t1:" in result.out
    assert f"remove Sandbox {run_id}-t1" in result.out
    assert f"remove Workspace {factory.workspace(run_id, 't1')}" in result.out
    assert f"remove branch factory/{run_id}--t1, its commits are in factory/{run_id}" in result.out
    assert "factory clean --yes" in result.out
    assert (factory.branches(), worktrees(factory), factory.all_events(run_id)) == (branches, trees, events)
    assert factory.workspace(run_id, "t1").is_dir()


def test_yes_removes_them_and_journals_each_step_to_its_run(factory: Harness) -> None:
    run_id = merged_run_with_leftovers(factory)

    result = factory.cli("clean", "--yes")

    assert result.code == 0, result.err
    assert factory.workspace(run_id, "t1").resolve() not in worktrees(factory)
    assert not factory.workspace(run_id, "t1").exists()
    assert f"factory/{run_id}--t1" not in factory.branches()
    assert factory.git("worktree", "list", "--porcelain").count("worktree ") == 1
    assert steps(factory, run_id)[-3:] == ["SandboxRemoved", "WorkspaceRemoved", "TicketBranchDeleted"]
    assert factory.events(run_id, "SandboxRemoved") == [{"ticket": "t1", "sandbox": f"{run_id}-t1"}]
    assert factory.cli("clean").out == "Nothing to clean\n"


def test_workspace_with_uncommitted_changes_stays_and_the_output_names_the_reason(factory: Harness) -> None:
    run_id = merged_run_with_leftovers(factory)
    workspace = factory.workspace(run_id, "t1")
    (workspace / "mine.txt").write_text("not committed\n")

    result = factory.cli("clean", "--yes")

    assert result.code == 0, result.err
    assert f"kept Workspace {workspace}" in result.out
    assert "modified or untracked files" in result.out
    assert (workspace / "mine.txt").is_file()
    assert workspace.resolve() in worktrees(factory)
    assert f"factory/{run_id}--t1" in factory.branches()
    failed = factory.events(run_id, "CleanupFailed")[-1]
    assert (failed["ticket"], failed["step"]) == ("t1", "workspace")


def test_workspace_with_uncommitted_changes_is_planned_to_stay_and_changes_nothing(factory: Harness) -> None:
    run_id = merged_run_with_leftovers(factory)
    workspace = factory.workspace(run_id, "t1")
    (workspace / "mine.txt").write_text("not committed\n")
    branches, trees, events = factory.branches(), worktrees(factory), factory.all_events(run_id)

    result = factory.cli("clean")

    assert result.code == 0, result.err
    assert f"keep Workspace {workspace} and its branch, it has uncommitted changes: mine.txt" in result.out
    assert f"remove Workspace {workspace}" not in result.out
    assert f"remove branch factory/{run_id}--t1" not in result.out
    assert (factory.branches(), worktrees(factory), factory.all_events(run_id)) == (branches, trees, events)


def test_branch_with_unsaved_work_goes_to_its_ref_before_it_is_deleted(factory: Harness) -> None:
    run_id = crashed_with_work(factory)
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


def test_branch_with_unsaved_work_is_planned_with_its_ref_and_changes_nothing(factory: Harness) -> None:
    run_id = crashed_with_work(factory)

    result = factory.cli("clean")

    assert f"goes to refs/factory/{run_id}/abandoned/t1 first" in result.out
    assert f"factory/{run_id}--t1" in factory.branches()
    assert factory.git("for-each-ref", "refs/factory") == ""


def test_a_run_that_still_runs_is_not_touched(factory: Harness) -> None:
    run_id = crashed_waiting_run(factory)
    branches, trees = factory.branches(), worktrees(factory)

    result = factory.cli("clean", "--yes")

    assert result.out == "Nothing to clean\n"
    assert (factory.branches(), worktrees(factory)) == (branches, trees)
    assert factory.workspace(run_id, "t1").is_dir()

    factory.clock.sleep(STALE.total_seconds())

    assert f"Run {run_id} (orphaned), Ticket t1:" in factory.cli("clean").out


def test_main_the_integration_branch_owner_branches_and_foreign_worktrees_do_not_change(factory: Harness) -> None:
    run_id = crashed_with_work(factory)
    foreign = factory.root / "foreign"
    factory.git("worktree", "add", "-q", "-b", "owner/work", str(foreign))
    factory.git("branch", f"factory/{run_id}-owner", "main")
    before = {k: v for k, v in factory.branches().items() if k != f"factory/{run_id}--t1"}

    result = factory.cli("clean", "--yes")

    assert result.code == 0, result.err
    assert factory.branches() == before
    assert foreign.resolve() in worktrees(factory)
    assert git(foreign, "rev-parse", "--abbrev-ref", "HEAD") == "owner/work"


def test_sandbox_that_does_not_stop_keeps_the_workspace_and_branch(factory: Harness) -> None:
    run_id = merged_run_with_leftovers(factory)
    factory.sandbox.refuse_remove = True

    result = factory.cli("clean", "--yes")

    assert result.code == 0, result.err
    assert f"kept Sandbox {run_id}-t1: Sandbox {run_id}-t1 did not stop" in result.out
    assert factory.workspace(run_id, "t1").resolve() in worktrees(factory)
    assert f"factory/{run_id}--t1" in factory.branches()


def test_it_removes_the_sandbox_through_the_sbx_cli(factory: Harness) -> None:
    factory.project["sandbox"] = {"kind": "sbx"}
    run_id = crashed_with_work(factory)

    result = factory.cli("clean", "--yes")

    assert result.code == 0, result.err
    assert f"removed Sandbox {run_id}-t1" in result.out
    assert [call[-1] for call in factory.sbx.calls("rm")] == [f"{run_id}-t1"]
    assert not factory.workspace(run_id, "t1").exists()


def test_a_sandbox_that_is_already_gone_is_not_a_leftover(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO
    run_id = run_id_of(factory.run(spec).out)

    assert factory.cli("clean").out == "Nothing to clean\n"
    assert factory.events(run_id, "SandboxRemoved")
    with pytest.raises(SandboxError):
        factory.sandbox.exec(f"{run_id}-t1", ["true"], stdout=factory.root / "out.txt")
