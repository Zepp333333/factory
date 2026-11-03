"""Operations carry out the Operations of a Pipeline on a Ticket's branch, and end a Ticket by cleaning it up.

`sync` brings the Integration branch into the Ticket branch, `merge` fast-forwards the Integration branch to the
Ticket, and `file_issues` files the issues the Ticket's Stages reported.
A merged or abandoned Ticket has its Sandbox, Workspace and branch removed, and an abandoned one keeps its head
under a ref.
"""

from dataclasses import asdict
from typing import Any

from factory.core.events import Event, applies, measures, nothing, times
from factory.core.git import GitError, MergeConflict
from factory.core.names import abandoned_ref, sandbox_name
from factory.core.states import TicketState
from factory.journal.run_state import Conflict, Landing, RunMemory
from factory.pipeline.pipeline import Operation
from factory.run.isolation import CleanupEvents
from factory.run.issue_filing import IssueFiling
from factory.run.live import LiveTicket
from factory.run.run_context import RunContext
from factory.run.run_tools import RunTools
from factory.views.artifacts import write_json_once
from factory.views.metrics import MetricsMemory, size
from factory.views.timeline import TimelineMemory


class Operations:
    """Operations runs the Operations of a Ticket and ends the Tickets that merged or were abandoned."""

    def __init__(self, context: RunContext, tools: RunTools) -> None:
        self.append = tools.append
        self.ports = tools.ports
        self.git = tools.git
        self.isolation = tools.attempts.isolation
        self.context = context
        self.issue_filing = IssueFiling(context, tools)

    @property
    def waiting(self) -> bool:
        """Tells whether an Operation waits to try the Tracker again, so the Run must not end yet."""
        return bool(self.issue_filing.failures)

    def operate(self, ticket: LiveTicket, operation: Operation) -> tuple[str, dict[str, Any] | None] | None:
        """Carries out the Operation and returns its outcome with the artifact it left, or None while it waits."""
        if operation.kind == "file_issues":
            return self.issue_filing.file(ticket, operation)
        operate = {"sync": self._sync, "merge": self._merge}[operation.kind]
        return operate(ticket, operation, self.git.resolve(self.context.integration))

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
            {**asdict(landing), "ticket_file": str(self.context.run_dir.ticket(landing.ticket))}
            for landing in self.context.memory.landings
            if not self.git.is_ancestor(landing.after, ticket.base)
        ]
        conflicts = sum(a["kind"] == "operation" and a["node"] == operation.name for a in ticket.artifacts)
        path = self.context.run_dir.ticket_dir(ticket.id) / operation.name / f"conflict-{conflicts + 1}.json"
        content = {"files": files, "base": ticket.base, "head": head, "tip": tip, "merged_since_base": merged}
        write_json_once(path, content)
        return {"kind": "operation", "node": operation.name, "path": str(path)}

    def _merge(self, ticket: LiveTicket, operation: Operation, tip: str) -> tuple[str, dict[str, Any] | None]:
        """Fast-forwards the Integration branch to the Ticket branch's head, and gives `merged`."""
        head = self.git.resolve(ticket.branch)
        self.git.fast_forward(self.context.integration, head, tip)
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
        ref = abandoned_ref(self.context.run_id, ticket.id)
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
        sandbox = sandbox_name(self.context.run_id, ticket.id)
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
        self.isolation.clean_up(sandbox, ticket.workspace, ticket.branch, commit, events)

    def cleanup_failed(self, ticket: LiveTicket, step: str, error: Exception) -> None:
        self.append(Event.CleanupFailed, {"ticket": ticket.id, "step": step, "reason": str(error)})


# What the events of an Operation and its clean-up tell the readers of the Journal.

applies(
    Event.AbandonedWithoutWork,
    Event.AbandonedWithoutWorkspace,
    Event.CleanupFailed,
    Event.SandboxLeftBehind,
    Event.SandboxRemoved,
    Event.TicketBranchDeleted,
    Event.WorkspaceRemoved,
)(nothing)


@applies(Event.OperationFinished)
def _apply_operation_finished(memory: RunMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is None:
        return
    if payload["kind"] == "sync" and payload["outcome"] == "clean":
        ticket.synced = payload["before"]
    elif payload["kind"] == "file_issues":
        if payload["outcome"] in ("refused", "failed"):
            ticket.artifacts.append(payload["artifact"])
    elif payload["kind"] == "sync":
        ticket.conflict = Conflict(payload["before"], payload["files"])
        ticket.artifacts.append(payload["artifact"])
    elif payload["outcome"] == "merged":
        ticket.state, ticket.merged_commit = TicketState.MERGED, payload["after"]
        memory.landings.append(Landing(ticket.id, payload["before"], payload["after"]))


@applies(Event.AbandonedBranchSaved)
def _apply_abandoned_branch_saved(memory: RunMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is not None:
        ticket.kept = True


@times(Event.OperationFinished)
def _time_operation_finished(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    if (entry := memory.entry(payload)) is None:
        return
    entry.outcome = payload["outcome"]
    entry.details = [", ".join(payload["files"])] if "files" in payload else []
    if "issues" in payload:
        entry.details = [", ".join(f"#{issue['number']}" for issue in payload["issues"])]


@measures(Event.OperationFinished)
def _measure_operation_finished(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is None:
        return
    ticket.work_s += payload.get("duration") or 0
    if payload["outcome"] == "merged" and "files_changed" in payload:
        ticket.merge_change = size(payload["node"], payload)
