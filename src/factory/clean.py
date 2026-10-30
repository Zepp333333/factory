"""`factory clean` finds what Runs that are no longer running left behind, and removes it.

A leftover is the Sandbox, the Workspace or the branch of a Ticket in a Run that ended or whose heartbeat went stale.
It removes them with the operations the cleanup after `merged` and `abandoned` uses, and journals every step to its Run.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.config import Project
from factory.events import Event
from factory.git import GitError, HostGit
from factory.journal import Journal
from factory.names import abandoned_ref, integration_branch, sandbox_name, ticket_branch
from factory.sandbox import Sandbox, SandboxError
from factory.workbench import Workbench, WorkbenchError


@dataclass(frozen=True)
class Leftover:
    """A Leftover is what one Ticket of a Run left behind, where a part that is gone is None."""

    ticket: str
    sandbox: str | None
    workspace: Path | None
    branch: str | None


class Cleaner:
    """A Cleaner finds and removes the leftovers of one Run that is no longer running."""

    def __init__(
        self, journal: Journal, home: Path, run_id: str, project: Project, sandbox: Sandbox, workbench: Workbench
    ) -> None:
        self.journal = journal
        self.run_id = run_id
        self.project = project
        self.sandbox = sandbox
        self.workbench = workbench
        self.git = HostGit(project.repo)
        self.workspaces = home / "workspaces" / run_id
        self.branch_prefix = ticket_branch(run_id, "")

    def find(self) -> list[Leftover]:
        """Returns the leftovers of the Run's Tickets, in the order of their ids.

        A Workspace is a directory under the Run's folder in `workspaces` or a worktree of the Target project there,
        a branch is `factory/<run-id>--<ticket>`, and a Sandbox is named after the Run and the Ticket.
        Worktrees and branches that are not in these places are not the factory's and stay out.
        """
        root = self.workspaces.resolve()
        workspaces = {path.name for path in self.workspaces.iterdir()} if self.workspaces.is_dir() else set()
        workspaces |= {path.name for path in self.git.worktrees() if path.parent.resolve() == root}
        branches = {name.removeprefix(self.branch_prefix) for name in self.git.branches(self.branch_prefix)}
        tickets = workspaces | branches | {t.id for t in self.journal.tickets(self.run_id)}
        found = []
        for ticket in sorted(tickets):
            path = self.workspaces / ticket
            name = sandbox_name(self.run_id, ticket)
            sandbox = name if self._sandbox_exists(name, path) else None
            workspace = path if ticket in workspaces else None
            branch = ticket_branch(self.run_id, ticket) if ticket in branches else None
            if sandbox or workspace or branch:
                found.append(Leftover(ticket, sandbox, workspace, branch))
        return found

    def plan(self, leftover: Leftover) -> list[str]:
        """Returns what `clean` would remove of the leftover and why, and changes nothing."""
        lines = []
        if leftover.sandbox:
            lines.append(f"remove Sandbox {leftover.sandbox}")
        if leftover.workspace:
            changes = self._uncommitted(leftover.workspace)
            if changes:
                lines.append(
                    f"keep Workspace {leftover.workspace} and its branch, "
                    f"it has uncommitted changes: {', '.join(changes)}"
                )
                return lines
            lines.append(f"remove Workspace {leftover.workspace}")
        if leftover.branch:
            head = self.git.resolve(leftover.branch)
            into = self._integrated(head)
            if into:
                lines.append(f"remove branch {leftover.branch}, its commits are in {into}")
            else:
                ref = abandoned_ref(self.run_id, leftover.ticket)
                lines.append(
                    f"remove branch {leftover.branch}, its commits are not in {self._targets()}, "
                    f"so its head {head} goes to {ref} first"
                )
        return lines

    def clean(self, leftover: Leftover) -> list[str]:
        """Removes the leftover step by step, journals each step and returns what happened.

        A step that fails is journaled with its reason and keeps what the steps after it would remove.
        The Workspace goes without `--force`, so one with uncommitted changes stays with its branch.
        A branch whose commits are in neither the Integration branch nor the base ref is saved under its ref first.
        """
        ticket = leftover.ticket
        lines: list[str] = []

        def append(type: Event, payload: dict[str, Any]) -> None:
            self.journal.append(self.run_id, type, {"ticket": ticket, **payload})

        def failed(step: str, what: str, error: Exception) -> list[str]:
            append(Event.CleanupFailed, {"step": step, "reason": str(error)})
            lines.append(f"kept {what}: {error}")
            return lines

        if leftover.sandbox:
            try:
                left_behind = self.sandbox.remove(leftover.sandbox)
            except (SandboxError, OSError) as error:
                return failed("sandbox", f"Sandbox {leftover.sandbox}", error)
            if left_behind is None:
                append(Event.SandboxRemoved, {"sandbox": leftover.sandbox})
                lines.append(f"removed Sandbox {leftover.sandbox}")
            else:
                append(Event.SandboxLeftBehind, {"sandbox": leftover.sandbox, "reason": left_behind})
                lines.append(f"left Sandbox {leftover.sandbox} behind: {left_behind}")
        if leftover.workspace:
            try:
                self.git.pin(leftover.workspace, ticket_branch(self.run_id, ticket))
                self.workbench.remove_workspace(self.project.repo, leftover.workspace)
            except (GitError, WorkbenchError, OSError) as error:
                return failed("workspace", f"Workspace {leftover.workspace}", error)
            append(Event.WorkspaceRemoved, {"workspace": str(leftover.workspace)})
            lines.append(f"removed Workspace {leftover.workspace}")
        if leftover.branch:
            step = "branch"
            try:
                head = self.git.resolve(leftover.branch)
                if self._integrated(head) is None:
                    step = "ref"
                    ref = abandoned_ref(self.run_id, ticket)
                    self.git.keep(ref, head)
                    append(Event.AbandonedBranchSaved, {"ref": ref, "commit": head})
                    lines.append(f"saved {leftover.branch} under {ref}")
                    step = "branch"
                self.git.delete_branch(leftover.branch, head)
            except GitError as error:
                return failed(step, f"branch {leftover.branch}", error)
            append(Event.TicketBranchDeleted, {"branch": leftover.branch, "commit": head})
            lines.append(f"removed branch {leftover.branch}")
        return lines

    def _sandbox_exists(self, name: str, workspace: Path) -> bool:
        try:
            return self.sandbox.attach(name, workspace)
        except (SandboxError, OSError):
            return False

    def _uncommitted(self, workspace: Path) -> list[str]:
        try:
            return self.git.uncommitted(workspace)
        except GitError:
            return []

    def _integrated(self, head: str) -> str | None:
        """Returns the first of the Integration branch and the base ref that holds `head`, or None."""
        for ref in (integration_branch(self.run_id), self.project.base):
            try:
                if self.git.is_ancestor(head, self.git.resolve(ref)):
                    return ref
            except GitError:
                continue
        return None

    def _targets(self) -> str:
        return f"{integration_branch(self.run_id)} or {self.project.base}"
