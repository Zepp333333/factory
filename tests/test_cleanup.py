import os
from pathlib import Path

import pytest

from factory.ports.sandbox import SandboxError
from harness import AgentScript, Harness, run_id_of, ticket_text

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
AFTER = "---\ntitle: Greet again\nblocked_by: [t1]\n---\nWrite hello.txt again.\n"
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "wrote hello.txt"})
CLEANUP = ["SandboxRemoved", "WorkspaceRemoved", "TicketBranchDeleted", "CleanupFailed"]


def worktrees(factory: Harness) -> list[Path]:
    lines = factory.git("worktree", "list", "--porcelain").splitlines()
    return [Path(line.removeprefix("worktree ")).resolve() for line in lines if line.startswith("worktree ")]


def cleanup_events(factory: Harness, run_id: str, ticket: str) -> list[str]:
    return [type for type, payload in factory.all_events(run_id) if type in CLEANUP and payload["ticket"] == ticket]


def is_ancestor(factory: Harness, commit: str, ref: str) -> bool:
    return factory.git("merge-base", commit, ref) == commit


def test_merged_tickets_are_cleaned_one_after_another_and_leave_only_the_integration_branch(
    factory: Harness,
) -> None:
    pid_file = factory.root / "lingering.pid"
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET, "t2": AFTER})
    factory.executor.scripts["implement"] = HELLO
    factory.executor.ticket_scripts[("t1", "implement")] = AgentScript(
        files=HELLO.files, run=[f"sleep 60 >/dev/null 2>&1 & echo $! > {pid_file}"], output=HELLO.output
    )
    before = factory.branches()

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    integration = f"factory/{run_id}"
    for ticket in ("t1", "t2"):
        workspace = factory.workspace(run_id, ticket)
        assert f"factory/{run_id}--{ticket}" not in factory.branches()
        assert not workspace.exists()
        with pytest.raises(SandboxError):
            factory.sandbox.exec(f"{run_id}-{ticket}", ["true"], stdout=factory.root / "out.txt")
        assert cleanup_events(factory, run_id, ticket) == ["SandboxRemoved", "WorkspaceRemoved", "TicketBranchDeleted"]
    assert not factory.workspace(run_id, "t1").parent.exists()
    assert worktrees(factory) == [factory.repo.resolve()]
    assert factory.events(run_id, "SandboxRemoved") == [
        {"ticket": ticket, "sandbox": f"{run_id}-{ticket}"} for ticket in ("t1", "t2")
    ]
    assert factory.events(run_id, "WorkspaceRemoved") == [
        {"ticket": ticket, "workspace": str(factory.workspace(run_id, ticket))} for ticket in ("t1", "t2")
    ]
    deleted = factory.events(run_id, "TicketBranchDeleted")
    assert [(e["ticket"], e["branch"]) for e in deleted] == [(t, f"factory/{run_id}--{t}") for t in ("t1", "t2")]
    assert all(is_ancestor(factory, e["commit"], integration) for e in deleted)
    # The cleanup keeps the Integration branch, the owner's branches and the Artifacts.
    after = factory.branches()
    assert {name: after[name] for name in before} == before
    assert after.keys() - before.keys() == {integration}
    assert factory.git("show", f"{integration}:hello.txt") == "hello"
    for request in factory.executor.requests:
        assert (request.attempt_dir / "output.json").is_file()
        assert (request.attempt_dir / "attempt.json").is_file()
    assert (factory.run_dir(run_id) / "spec.md").is_file()
    # Removing the Sandbox stops what the agent left running in it.
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    # A Sandbox the cleanup removed is no leftover of `factory clean`.
    assert factory.cli("clean").out == "Nothing to clean\n"
    # A Run without a terminal on its stdin sends no OSC 7501 probe.
    assert "\x1b]7501" not in result.out


def abandon_after_gate(factory: Harness) -> Path:
    """Sets up a Ticket that commits hello.txt and is abandoned when the Gate `build` fails."""
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {
        "build": {"kind": "command", "with": {"run": "false"}, "next": {"pass": "merge", "fail": "abandoned"}}
    }
    factory.executor.scripts["implement"] = HELLO
    return factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})


def test_abandoned_tickets_keep_their_work_under_a_ref_named_by_status_and_the_pr_and_leave_nothing_else(
    factory: Harness,
) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {
        "build": {
            "kind": "command",
            "with": {"run": "test -f ok.txt"},
            "next": {"pass": "catch_up", "fail": "abandoned"},
        }
    }
    factory.executor.ticket_scripts[("t1", "implement")] = HELLO
    factory.executor.ticket_scripts[("t2", "implement")] = AgentScript(output={"handoff": "nothing to do"})
    factory.executor.ticket_scripts[("t3", "implement")] = AgentScript(
        files={"ok.txt": "ok\n"}, output={"summary": "Add ok.txt", "handoff": "done"}
    )
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET, "t2": ticket_text("Idle"), "t3": TICKET})
    before = factory.branches()

    result = factory.run(spec)

    assert result.code == 1, result.out
    run_id = run_id_of(result.out)
    integration = f"factory/{run_id}"
    ref = f"refs/factory/{run_id}/abandoned/t1"
    head = factory.git("rev-parse", ref)
    # t1 committed hello.txt, so its head stays under its ref and nothing else of it is left.
    assert factory.git("show", f"{ref}:hello.txt") == "hello"
    assert head != factory.git("rev-parse", "main")
    assert "hello.txt" not in factory.git("ls-tree", "--name-only", integration).split()
    assert factory.events(run_id, "AbandonedBranchSaved") == [{"ticket": "t1", "ref": ref, "commit": head}]
    assert [e for e in factory.events(run_id, "TicketBranchDeleted") if e["ticket"] == "t1"] == [
        {"ticket": "t1", "branch": f"factory/{run_id}--t1", "commit": head}
    ]
    # t2 committed nothing, so it gets no ref and is still cleaned up.
    [none] = factory.events(run_id, "AbandonedWithoutWork")
    assert (none["ticket"], none["commit"]) == ("t2", factory.git("rev-parse", "main"))
    assert factory.git("for-each-ref", "--format=%(refname)", "refs/factory") == ref
    for ticket in ("t1", "t2", "t3"):
        workspace = factory.workspace(run_id, ticket)
        assert cleanup_events(factory, run_id, ticket) == ["SandboxRemoved", "WorkspaceRemoved", "TicketBranchDeleted"]
        assert f"factory/{run_id}--{ticket}" not in factory.branches()
        assert workspace.resolve() not in worktrees(factory)
        assert not workspace.exists()
        with pytest.raises(SandboxError):
            factory.sandbox.exec(f"{run_id}-{ticket}", ["true"], stdout=factory.root / "out.txt")
    after = factory.branches()
    assert {name: after[name] for name in before} == before
    assert after.keys() - before.keys() == {integration}
    assert (factory.run_dir(run_id) / "spec.md").is_file()
    lines = factory.cli("status", run_id).out.splitlines()
    assert f"Ticket t1 abandoned, its work is in {ref}" in lines
    assert not any(line.startswith("Ticket t2 abandoned") for line in lines)
    body = (factory.run_dir(run_id) / "pull_request.md").read_text()
    local = "a ref in the repository of whoever ran the Run, not pushed"
    assert f"- t1 Greet: abandoned, its work is in `{ref}`, {local}\n" in body


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
    assert cleanup_events(factory, run_id, "t1") == ["CleanupFailed"]
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
    assert cleanup_events(factory, run_id, "t1") == ["SandboxRemoved", "WorkspaceRemoved", "CleanupFailed"]
    [failed] = factory.events(run_id, "CleanupFailed")
    assert failed["step"] == "branch"
    assert factory.git("rev-parse", f"factory/{run_id}--t1") != factory.git("rev-parse", ref)
    assert factory.git("log", "-1", "--format=%s", f"factory/{run_id}--t1") == "late"
    assert factory.git("show", f"{ref}:hello.txt") == "hello"
    assert factory.cli("status", run_id).out.splitlines()[2].split()[6] == "abandoned"
