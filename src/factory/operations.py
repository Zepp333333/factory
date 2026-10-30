"""Operations carry out the Operations of a Pipeline on a Ticket's branch, and end a Ticket by cleaning it up.

`sync` brings the Integration branch into the Ticket branch and `merge` fast-forwards the Integration branch to the
Ticket.
A merged or abandoned Ticket has its Sandbox, Workspace and branch removed, and an abandoned one keeps its head
under a ref.
"""

from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Protocol

from factory.artifacts import RunDir, write_json_once
from factory.attempt import Attempts, CleanupEvents, Ports
from factory.events import Event
from factory.git import GitError, HostGit, MergeConflict
from factory.live import LiveTicket
from factory.names import abandoned_ref, sandbox_name
from factory.pipeline import Operation
from factory.run_state import Landing


class Scope(Protocol):
    """A Scope is the Run whose Tickets are operated on: its id, its directory, its Integration branch and merges."""

    run_id: str
    run_dir: RunDir
    integration: str

    @property
    def landings(self) -> list[Landing]: ...


class Operations:
    """Operations runs the Operations of a Ticket and ends the Tickets that merged or were abandoned.

    `append` journals an event and applies it to the Run state.
    """

    def __init__(
        self,
        append: Callable[[Event, dict[str, Any]], None],
        ports: Ports,
        git: HostGit,
        attempts: Attempts,
        scope: Scope,
    ) -> None:
        self.append = append
        self.ports = ports
        self.git = git
        self.attempts = attempts
        self.scope = scope

    def operate(self, ticket: LiveTicket, operation: Operation) -> tuple[str, dict[str, Any] | None]:
        operate = {"sync": self._sync, "merge": self._merge}[operation.kind]
        return operate(ticket, operation, self.git.resolve(self.scope.integration))

    def _sync(self, ticket: LiveTicket, operation: Operation, tip: str) -> tuple[str, dict[str, Any] | None]:
        """Brings the Integration branch's tip into the Ticket branch, and gives `clean` or `conflict`.

        A conflict stays in the Workspace as markers and `MERGE_HEAD` and leaves an artifact that lists
        the conflicting files and the Tickets merged since the Ticket's base.

        The tip cannot move while this Ticket holds the window, so the Gates after it see the tree `merge` lands.
        """
        assert ticket.workspace is not None
        try:
            after = self.git.catch_up(ticket.workspace, ticket.branch, tip)
        except MergeConflict as error:
            head = self.git.resolve(ticket.branch)
            artifact = self._conflict_artifact(ticket, operation, tip, head, error.files)
            self._operation_finished(
                ticket, operation, tip, "conflict", head, reason=str(error), artifact=artifact, files=error.files
            )
            return "conflict", artifact
        self._operation_finished(ticket, operation, tip, "clean", after)
        return "clean", None

    def _conflict_artifact(
        self, ticket: LiveTicket, operation: Operation, tip: str, head: str, files: list[str]
    ) -> dict[str, Any]:
        """Writes what `resolve` needs to know: the conflicting files and the Tickets merged after the Ticket's base.

        Each merged Ticket comes with its commit range and its rendered Ticket file.
        """
        merged = [
            {**asdict(landing), "ticket_file": str(self.scope.run_dir.ticket(landing.ticket))}
            for landing in self.scope.landings
            if not self.git.is_ancestor(landing.after, ticket.base)
        ]
        conflicts = sum(a["kind"] == "operation" and a["node"] == operation.name for a in ticket.artifacts)
        path = self.scope.run_dir.ticket_dir(ticket.id) / operation.name / f"conflict-{conflicts + 1}.json"
        content = {"files": files, "base": ticket.base, "head": head, "tip": tip, "merged_since_base": merged}
        write_json_once(path, content)
        return {"kind": "operation", "node": operation.name, "path": str(path)}

    def _merge(self, ticket: LiveTicket, operation: Operation, tip: str) -> tuple[str, dict[str, Any] | None]:
        """Fast-forwards the Integration branch to the Ticket branch's head, and gives `merged`."""
        head = self.git.resolve(ticket.branch)
        self.git.fast_forward(self.scope.integration, head, tip)
        self._operation_finished(ticket, operation, tip, "merged", head, **self.git.size(tip, head))
        return "merged", None

    def _operation_finished(
        self, ticket: LiveTicket, operation: Operation, before: str, outcome: str, after: str, **extra: Any
    ) -> None:
        payload = {"ticket": ticket.id, "node": operation.name, "kind": operation.kind, "before": before}
        duration = ticket.seconds_in_node(self.ports.clock.now())
        self.append(
            Event.OperationFinished, {**payload, "outcome": outcome, "after": after, "duration": duration, **extra}
        )

    def abandon(self, ticket: LiveTicket) -> None:
        """Saves the head of an abandoned Ticket's branch under its ref, then cleans the Ticket up.

        A head that cannot be saved is journaled with its reason and keeps the Sandbox, Workspace and branch,
        because the branch would be the only place left to find the work.
        A Ticket that never got a Workspace has nothing to save or clean up, and one whose branch still points at its
        base holds no work, so it gets no ref.
        """
        if ticket.workspace is None:
            self.append(Event.AbandonedWithoutWorkspace, {"ticket": ticket.id})
            return
        ref = abandoned_ref(self.scope.run_id, ticket.id)
        try:
            head = self.git.resolve(ticket.branch)
            if head == ticket.base:
                self.append(Event.AbandonedWithoutWork, {"ticket": ticket.id, "commit": head})
                self.clean_up(ticket, head)
                return
            self.git.keep(ref, head)
        except GitError as error:
            self.cleanup_failed(ticket, "ref", error)
            return
        self.append(Event.AbandonedBranchSaved, {"ticket": ticket.id, "ref": ref, "commit": head})
        self.clean_up(ticket, head)

    def clean_up(self, ticket: LiveTicket, commit: str) -> None:
        """Removes the Sandbox, the Workspace and the branch of a merged or abandoned Ticket, in that order.

        A step that fails is journaled with its reason and keeps what the steps after it would remove,
        and the Ticket stays merged.
        A Sandbox that stays behind stopped for good runs nothing more, so the Workspace and branch still go.
        The branch goes only while it still points at `commit`, the merged or saved one,
        so no later commit is lost with it.
        """
        assert ticket.workspace is not None
        sandbox = sandbox_name(self.scope.run_id, ticket.id)
        events = CleanupEvents(
            removed=lambda: self.append(Event.SandboxRemoved, {"ticket": ticket.id, "sandbox": sandbox}),
            left_behind=lambda reason: self.append(
                Event.SandboxLeftBehind, {"ticket": ticket.id, "sandbox": sandbox, "reason": reason}
            ),
            workspace_removed=lambda: self.append(
                Event.WorkspaceRemoved, {"ticket": ticket.id, "workspace": str(ticket.workspace)}
            ),
            branch_deleted=lambda: self.append(
                Event.TicketBranchDeleted, {"ticket": ticket.id, "branch": ticket.branch, "commit": commit}
            ),
            failed=lambda step, error: self.cleanup_failed(ticket, step, error),
        )
        self.attempts.clean_up(sandbox, ticket.workspace, ticket.branch, commit, events)

    def cleanup_failed(self, ticket: LiveTicket, step: str, error: Exception) -> None:
        self.append(Event.CleanupFailed, {"ticket": ticket.id, "step": step, "reason": str(error)})
