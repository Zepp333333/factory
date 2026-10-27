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


def abandon_after_gate(factory: Harness) -> Path:
    """Sets up a Ticket that commits hello.txt and is abandoned when the Gate `build` fails."""
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {
        "build": {"kind": "command", "with": {"run": "false"}, "next": {"pass": "merge", "fail": "abandoned"}}
    }
    factory.executor.scripts["implement"] = HELLO
    return factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})


def test_abandoned_ticket_keeps_its_head_under_a_ref_and_leaves_nothing_else(factory: Harness) -> None:
    spec = abandon_after_gate(factory)
    before = factory.branches()

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    workspace = factory.workspace(run_id, "t1")
    ref = f"refs/factory/{run_id}/abandoned/t1"
    assert factory.git("show", f"{ref}:hello.txt") == "hello"
    assert factory.git("rev-parse", ref) != factory.git("rev-parse", "main")
    assert f"factory/{run_id}--t1" not in factory.branches()
    assert workspace.resolve() not in worktrees(factory)
    assert not workspace.exists()
    with pytest.raises(SandboxError):
        factory.sandbox.exec(f"{run_id}-t1", ["true"], stdout=factory.root / "out.txt")
    after = factory.branches()
    assert {name: after[name] for name in before} == before
    assert after.keys() - before.keys() == {f"factory/{run_id}"}
    assert factory.git("rev-parse", f"factory/{run_id}") == factory.git("rev-parse", "main")
    assert (factory.run_dir(run_id) / "spec.md").is_file()


def test_abandoned_ticket_cleanup_is_journaled_step_by_step(factory: Harness) -> None:
    spec = abandon_after_gate(factory)

    run_id = run_id_of(factory.run(spec).out)

    head = factory.git("rev-parse", f"refs/factory/{run_id}/abandoned/t1")
    assert cleanup_events(factory, run_id) == ["SandboxRemoved", "WorkspaceRemoved", "TicketBranchDeleted"]
    assert factory.events(run_id, "AbandonedBranchSaved") == [
        {"ticket": "t1", "ref": f"refs/factory/{run_id}/abandoned/t1", "commit": head}
    ]
    assert factory.events(run_id, "TicketBranchDeleted") == [
        {"ticket": "t1", "branch": f"factory/{run_id}--t1", "commit": head}
    ]


def test_abandoned_workspace_with_uncommitted_edits_stays_and_the_outcome_holds(factory: Harness) -> None:
    spec = abandon_after_gate(factory)
    factory.workbench.late_files = {"late.txt": "written after the abandon\n"}

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    workspace = factory.workspace(run_id, "t1")
    assert (workspace / "late.txt").is_file()
    assert workspace.resolve() in worktrees(factory)
    assert f"factory/{run_id}--t1" in factory.branches()
    assert factory.git("rev-parse", f"refs/factory/{run_id}/abandoned/t1") == factory.git(
        "rev-parse", f"factory/{run_id}--t1"
    )
    assert cleanup_events(factory, run_id) == ["SandboxRemoved", "CleanupFailed"]
    [failed] = factory.events(run_id, "CleanupFailed")
    assert (failed["step"], "modified or untracked files" in failed["reason"]) == ("workspace", True)
    status = factory.cli("status", run_id).out.splitlines()
    assert status[0] == f"Run {run_id}  partial  factory/{run_id}"
    assert status[2].split()[-1] == "abandoned"


def test_sandbox_that_does_not_stop_does_not_change_the_abandoned_outcome(factory: Harness) -> None:
    spec = abandon_after_gate(factory)
    factory.sandbox.refuse_remove = True

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    assert factory.workspace(run_id, "t1").resolve() in worktrees(factory)
    assert f"factory/{run_id}--t1" in factory.branches()
    assert f"refs/factory/{run_id}/abandoned/t1" in factory.git("for-each-ref", "refs/factory")
    assert [e["step"] for e in factory.events(run_id, "CleanupFailed")] == ["sandbox"]


def test_status_names_the_ref_that_holds_an_abandoned_ticket(factory: Harness) -> None:
    spec = abandon_after_gate(factory)

    run_id = run_id_of(factory.run(spec).out)

    lines = factory.cli("status", run_id).out.splitlines()
    assert lines[-1] == f"Ticket t1 abandoned, its work is in refs/factory/{run_id}/abandoned/t1"


def test_ticket_abandoned_without_a_commit_gets_no_ref_and_is_still_cleaned_up(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {
        "build": {"kind": "command", "with": {"run": "false"}, "next": {"pass": "merge", "fail": "abandoned"}}
    }
    factory.executor.scripts["implement"] = AgentScript(output={"handoff": "nothing to do"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    assert factory.git("for-each-ref", "refs/factory") == ""
    assert factory.events(run_id, "AbandonedBranchSaved") == []
    [none] = factory.events(run_id, "AbandonedWithoutWork")
    assert none["commit"] == factory.git("rev-parse", "main")
    assert cleanup_events(factory, run_id) == ["SandboxRemoved", "WorkspaceRemoved", "TicketBranchDeleted"]
    assert f"factory/{run_id}--t1" not in factory.branches()
    assert not any(line.startswith("Ticket t1 abandoned") for line in factory.cli("status", run_id).out.splitlines())


def test_ref_that_cannot_be_saved_keeps_the_sandbox_workspace_and_branch(factory: Harness) -> None:
    spec = abandon_after_gate(factory)
    # The Probe runs in the Workspace, which shares refs with the Target project, and plants the ref at another commit.
    plant = (
        "RUN=$(basename $(dirname $PWD)); git update-ref refs/factory/$RUN/abandoned/t1 $(git rev-parse main); false"
    )
    factory.pipeline["gates"]["build"]["with"] = {"run": plant}

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    [failed] = factory.events(run_id, "CleanupFailed")
    assert failed["step"] == "ref"
    assert cleanup_events(factory, run_id) == ["CleanupFailed"]
    assert factory.events(run_id, "AbandonedBranchSaved") == []
    assert f"factory/{run_id}--t1" in factory.branches()
    assert factory.workspace(run_id, "t1").resolve() in worktrees(factory)
    assert factory.git("rev-parse", f"refs/factory/{run_id}/abandoned/t1") == factory.git("rev-parse", "main")


def test_branch_that_moved_after_the_ref_was_saved_is_not_deleted(factory: Harness) -> None:
    spec = abandon_after_gate(factory)
    factory.workbench.late_commit = True

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    ref = f"refs/factory/{run_id}/abandoned/t1"
    assert cleanup_events(factory, run_id) == ["SandboxRemoved", "WorkspaceRemoved", "CleanupFailed"]
    [failed] = factory.events(run_id, "CleanupFailed")
    assert failed["step"] == "branch"
    assert factory.git("rev-parse", f"factory/{run_id}--t1") != factory.git("rev-parse", ref)
    assert factory.git("log", "-1", "--format=%s", f"factory/{run_id}--t1") == "late"
    assert factory.git("show", f"{ref}:hello.txt") == "hello"
    assert factory.cli("status", run_id).out.splitlines()[2].split()[-1] == "abandoned"


def test_abandoned_ticket_is_cleaned_up_through_the_sbx_cli(factory: Harness) -> None:
    factory.project["sandbox"] = {"kind": "sbx"}
    spec = abandon_after_gate(factory)

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    assert [call[-1] for call in factory.sbx.calls("rm")] == [f"{run_id}-t1"]
    assert cleanup_events(factory, run_id) == ["SandboxRemoved", "WorkspaceRemoved", "TicketBranchDeleted"]


def test_discarded_patch_of_a_failed_attempt_survives_the_cleanup(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["attempts"] = 1
    factory.executor.scripts["implement"] = AgentScript(files={"junk.txt": "half\n"}, fail="crash")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    [request] = factory.executor.requests
    assert "+half" in (request.attempt_dir / "discarded.patch").read_text()
    assert not factory.workspace(run_id, "t1").exists()


def test_pr_says_that_the_ref_of_an_abandoned_ticket_is_local(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {
        "build": {"kind": "command", "with": {"run": "test ! -f a.txt"}, "next": {"pass": "sync", "fail": "abandoned"}}
    }
    for name in ("a", "b"):
        factory.executor.ticket_scripts[(name, "implement")] = AgentScript(
            files={f"{name}.txt": f"{name}\n"}, output={"summary": f"Add {name}", "handoff": "done"}
        )
    spec = factory.write_spec("Files", "Two files.", {"a": TICKET, "b": TICKET})

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    body = (factory.run_dir(run_id) / "pull_request.md").read_text()
    ref = f"refs/factory/{run_id}/abandoned/a"
    local = "a ref in the repository of whoever ran the Run, not pushed"
    assert f"- a Greet: abandoned, its work is in `{ref}`, {local}\n" in body


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
