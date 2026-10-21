import os
from pathlib import Path

import pytest

from factory.sandbox import SandboxError
from harness import AgentScript, Harness, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
AFTER = "---\ntitle: Greet again\nblocked_by: [t1]\n---\nWrite hello.txt again.\n"
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "wrote hello.txt"})
CLEANUP = ["SandboxRemoved", "WorkspaceRemoved", "TicketBranchDeleted", "CleanupFailed"]


def worktrees(factory: Harness) -> list[Path]:
    lines = factory.git("worktree", "list", "--porcelain").splitlines()
    return [Path(line.removeprefix("worktree ")).resolve() for line in lines if line.startswith("worktree ")]


def cleanup_events(factory: Harness, run_id: str) -> list[str]:
    return [type for type, _ in factory.all_events(run_id) if type in CLEANUP]


def test_merged_ticket_leaves_no_branch_workspace_or_sandbox(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    workspace = factory.workspace(run_id, "t1")
    assert f"factory/{run_id}--t1" not in factory.branches()
    assert workspace.resolve() not in worktrees(factory)
    assert not workspace.exists()
    assert not workspace.parent.exists()
    with pytest.raises(SandboxError):
        factory.sandbox.exec(f"{run_id}-t1", ["true"], stdout=factory.root / "out.txt")
    assert cleanup_events(factory, run_id) == ["SandboxRemoved", "WorkspaceRemoved", "TicketBranchDeleted"]


def test_cleanup_keeps_the_integration_branch_owner_branches_and_run_dir(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO
    before = factory.branches()

    run_id = run_id_of(factory.run(spec).out)

    after = factory.branches()
    assert {name: after[name] for name in before} == before
    assert after.keys() - before.keys() == {f"factory/{run_id}"}
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    [request] = factory.executor.requests
    assert (request.attempt_dir / "output.json").is_file()
    assert (request.attempt_dir / "attempt.json").is_file()
    assert (factory.run_dir(run_id) / "spec.md").is_file()


def test_each_cleanup_step_is_journaled_with_what_it_removed(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO

    run_id = run_id_of(factory.run(spec).out)

    integration = factory.git("rev-parse", f"factory/{run_id}")
    assert factory.events(run_id, "SandboxRemoved") == [{"ticket": "t1", "sandbox": f"{run_id}-t1"}]
    assert factory.events(run_id, "WorkspaceRemoved") == [
        {"ticket": "t1", "workspace": str(factory.workspace(run_id, "t1"))}
    ]
    assert factory.events(run_id, "TicketBranchDeleted") == [
        {"ticket": "t1", "branch": f"factory/{run_id}--t1", "commit": integration}
    ]


def test_workspace_with_uncommitted_edits_stays_with_its_branch(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO
    factory.workbench.late_files = {"late.txt": "written after the merge\n"}

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    workspace = factory.workspace(run_id, "t1")
    assert (workspace / "late.txt").read_text() == "written after the merge\n"
    assert workspace.resolve() in worktrees(factory)
    assert f"factory/{run_id}--t1" in factory.branches()
    assert cleanup_events(factory, run_id) == ["SandboxRemoved", "CleanupFailed"]
    [failed] = factory.events(run_id, "CleanupFailed")
    assert failed["ticket"] == "t1"
    assert failed["step"] == "workspace"
    assert "modified or untracked files" in failed["reason"]
    assert factory.cli("status", run_id).out.splitlines()[0] == f"Run {run_id}  done  factory/{run_id}"
    assert factory.cli("status", run_id).out.splitlines()[2].split()[-1] == "merged"


def test_sandbox_that_does_not_stop_keeps_the_workspace_it_works_on(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO
    factory.sandbox.refuse_remove = True

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert factory.workspace(run_id, "t1").resolve() in worktrees(factory)
    assert f"factory/{run_id}--t1" in factory.branches()
    assert factory.events(run_id, "CleanupFailed") == [
        {"ticket": "t1", "step": "sandbox", "reason": f"Sandbox {run_id}-t1 did not stop"}
    ]


def test_failed_cleanup_of_one_ticket_does_not_stop_the_next(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET, "t2": AFTER})
    factory.executor.scripts["implement"] = HELLO
    factory.sandbox.refuse_remove = True

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert [row.split()[-1] for row in factory.cli("status", run_id).out.splitlines()[2:]] == ["merged", "merged"]
    assert [e["ticket"] for e in factory.events(run_id, "CleanupFailed")] == ["t1", "t2"]


def test_tickets_are_cleaned_one_after_another(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET, "t2": AFTER})
    factory.executor.scripts["implement"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert [e["ticket"] for e in factory.events(run_id, "TicketBranchDeleted")] == ["t1", "t2"]
    assert worktrees(factory) == [factory.repo.resolve()]


def test_abandoned_ticket_keeps_its_workspace_and_branch(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, fail="crash")

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    assert factory.workspace(run_id, "t1").resolve() in worktrees(factory)
    assert f"factory/{run_id}--t1" in factory.branches()
    assert cleanup_events(factory, run_id) == []


def test_removing_the_sandbox_stops_what_the_agent_left_running(factory: Harness) -> None:
    pid_file = factory.root / "lingering.pid"
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n"},
        run=[f"sleep 60 >/dev/null 2>&1 & echo $! > {pid_file}"],
        output={"summary": "Add hello.txt", "handoff": "wrote hello.txt"},
    )

    result = factory.run(spec)

    assert result.code == 0, result.out
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
