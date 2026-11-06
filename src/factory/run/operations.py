"""Operations carry out the Operations of a Pipeline on a Ticket's branch, and end a Ticket by cleaning it up.

`catch_up` brings the Integration branch into the Ticket branch, `merge` fast-forwards the Integration branch to the
Ticket, and `file_issues` files the issues the Ticket's Stages reported.
`assign`, `label` and `comment` act on the Tracker through its TicketBoard, in a Pipeline on the Ticket and in a Run
graph on each Ticket of the Run or on the Spec.
A merged or abandoned Ticket has its Sandbox, Workspace and branch removed, and an abandoned one keeps its head
under a ref.
"""

import zlib
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from factory.core.events import Event, applies, measures, nothing, times
from factory.core.git import GitError, HostGit, MergeConflict
from factory.core.names import abandoned_ref
from factory.core.states import TicketState
from factory.journal.artifacts import write_json_once
from factory.journal.metrics_memory import MetricsMemory, size
from factory.journal.run_state import Conflict, Landing, RunMemory
from factory.journal.timeline_memory import TimelineMemory
from factory.pipeline.pipeline import (
    ASSIGN,
    COMMENT,
    COMMENT_REFERENCES,
    ESCALATE,
    RETRY,
    TRACKER_OPERATIONS,
    WARN,
    Operation,
    RunNode,
    TrackerAction,
)
from factory.ports.run_ports import Ports
from factory.ports.tracker import IssueDesk, TicketBoard, TrackerError
from factory.run.isolation import CleanupEvents, Isolation
from factory.run.issue_filing import IssueFiling, issue_id
from factory.run.live import LiveTicket
from factory.run.run_context import Append, Display, RunContext

# An action on the Tracker that failed with `on_fail: retry` is tried again after this many seconds.
TRACKER_RETRY_SECONDS = 30

# What `RunMemory.acted` holds the results of the Run node's actions by, which no Ticket id is.
RUN_NODE_ACTS = ""


class TrackerOperations:
    """TrackerOperations carries out the Operations on the Tracker through its TicketBoard, one target at a time.

    The result of each action goes in the Journal by its target before the next one, so a Run that resumes does only
    those the Journal lacks, and a warned failure is such a result.
    `board` is what `factory check` makes sure a Pipeline or Run graph with such an Operation has.
    """

    def __init__(
        self,
        context: RunContext,
        append: Append,
        ports: Ports,
        view: Display,
        board: TicketBoard | None,
        beat: Callable[[], None],
    ) -> None:
        self.context = context
        self.append = append
        self.ports = ports
        self.view = view
        self.board = board
        self.beat = beat
        self.retry_at: dict[str, datetime] = {}

    @property
    def waiting(self) -> bool:
        """Tells whether a Ticket still at work waits to try the Tracker again, so the Run must not end yet."""
        return any(t.id in self.retry_at and t.state == TicketState.ACTIVE for t in self.context.tickets)

    def act_for_ticket(self, ticket: LiveTicket, action: TrackerAction, node: str) -> str | None:
        """Acts on the Ticket and returns the outcome, or None while it waits to try the Tracker again."""
        at = ticket.entered_at.isoformat() if ticket.entered_at else ""
        return self._act(ticket.id, action, node, [ticket.id], {"ticket": ticket.id}, at)

    def act_for_run(self, node: RunNode) -> str | None:
        """Acts on the Spec or on each Ticket of the Run and returns the outcome, or None while it waits to retry."""
        action = node.tracker
        assert action is not None, "Config proves that an Operation on the Tracker has its action"
        targets = [self._board.spec_id()] if action.on_spec else [t.id for t in self.context.snapshot.tickets]
        return self._act(RUN_NODE_ACTS, action, node.name, targets, {}, "")

    @property
    def _board(self) -> TicketBoard:
        # `factory check` and the start of `factory run` refuse an Operation on a Tracker without a TicketBoard.
        assert self.board is not None
        return self.board

    def _act(
        self, scope: str, action: TrackerAction, node: str, targets: list[str], extra: dict[str, str], visit: str
    ) -> str | None:
        """Carries out what the Journal lacks of `scope` until all is done, or the Tracker fails with `retry` or
        `escalate`.

        `visit` tells this entry into `node` from another, for the key of a comment.
        """
        now = self.ports.clock.now()
        if now < self.retry_at.get(scope, now):
            return None
        self.retry_at.pop(scope, None)
        for target in targets:
            for name in action.actions:
                if (target, name) in self.context.memory.acted.get(scope, ()):
                    continue
                # Each call may wait out its timeout on a Tracker that hangs, so the Run beats between them.
                self.beat()
                payload = {**extra, "node": node, "target": target, "action": name}
                try:
                    result = self._carry_out(action, name, target, node, visit)
                except (TrackerError, OSError) as error:
                    self._failed(action, payload, str(error))
                    if action.on_fail == ESCALATE:
                        return "failed"
                    if action.on_fail == RETRY:
                        self.retry_at[scope] = now + timedelta(seconds=TRACKER_RETRY_SECONDS)
                        return None
                    continue
                self.append(Event.TrackerActed, {**payload, "result": result})
        return "done"

    def _carry_out(self, action: TrackerAction, name: str, target: str, node: str, visit: str) -> str | None:
        """Carries out one action on the target and returns what the Tracker says of it, which is an account or None."""
        board = self._board
        if action.kind == ASSIGN:
            return board.assign(target)
        if action.kind == COMMENT:
            board.comment(target, _comment_key(self.context.run_id, node, target, visit), self._text(action, target))
            return None
        verb, _, label = name.partition(":")
        (board.add_label if verb == "add" else board.remove_label)(target, label)
        return None

    def _text(self, action: TrackerAction, target: str) -> str:
        """Fills in the references of the static text of a comment on the target."""
        reference = self.context.closing.reference
        values = {
            "run": self.context.run_id,
            "branch": self.context.integration,
            "spec": reference(self._board.spec_id()),
            "ticket": reference(target),
        }
        text = action.text
        for name in COMMENT_REFERENCES:
            text = text.replace(f"${{{name}}}", values[name])
        return text

    def _failed(self, action: TrackerAction, payload: dict[str, str], reason: str) -> None:
        self.append(Event.TrackerActionFailed, {**payload, "on_fail": action.on_fail, "reason": reason})
        shown = self.context.closing.reference(payload["target"])
        self.view.warn(f"{shown}: {payload['action']} failed: {reason}")


def _comment_key(run_id: str, node: str, target: str, visit: str) -> int:
    """Returns the key of a comment, which a repeat of it shares and which no event of the Journal has as its number."""
    return (1 << 32) + zlib.crc32(f"{run_id}/{node}/{target}/{visit}".encode())


class Operations:
    """Operations runs the Operations of a Ticket and ends the Tickets that merged or were abandoned.

    `desk` is the IssueDesk of the Tracker that `file_issues` files through, which `factory check` makes sure a
    Pipeline with `file_issues` has.
    """

    def __init__(
        self,
        context: RunContext,
        append: Append,
        ports: Ports,
        git: HostGit,
        isolation: Isolation,
        desk: IssueDesk | None,
        tracker_operations: TrackerOperations,
    ) -> None:
        self.append = append
        self.ports = ports
        self.git = git
        self.isolation = isolation
        self.context = context
        self.issue_filing = IssueFiling(context, append, ports, git, desk)
        self.tracker_operations = tracker_operations

    @property
    def waiting(self) -> bool:
        """Tells whether an Operation waits to try the Tracker again, so the Run must not end yet."""
        return bool(self.issue_filing.failures) or self.tracker_operations.waiting

    def operate(self, ticket: LiveTicket, operation: Operation) -> tuple[str, dict[str, Any] | None] | None:
        """Carries out the Operation and returns its outcome with the artifact it left, or None while it waits."""
        if operation.kind == "file_issues":
            return self.issue_filing.file(ticket, operation)
        if operation.tracker is not None:
            return self._act_on_tracker(ticket, operation, operation.tracker)
        operate = {"catch_up": self._catch_up, "merge": self._merge}[operation.kind]
        return operate(ticket, operation, self.git.resolve(self.context.integration))

    def _act_on_tracker(
        self, ticket: LiveTicket, operation: Operation, action: TrackerAction
    ) -> tuple[str, dict[str, Any] | None] | None:
        """Acts on the Ticket through the TicketBoard, and gives `done` or `failed`, or None while it waits."""
        outcome = self.tracker_operations.act_for_ticket(ticket, action, operation.name)
        if outcome is None:
            return None
        duration = ticket.seconds_in_node(self.ports.clock.now())
        self.append(
            Event.OperationFinished,
            {
                "ticket": ticket.id,
                "node": operation.name,
                "kind": operation.kind,
                "outcome": outcome,
                "duration": duration,
            },
        )
        return outcome, None

    def _catch_up(self, ticket: LiveTicket, operation: Operation, tip: str) -> tuple[str, dict[str, Any] | None]:
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
        sandbox = self.context.sandbox(ticket.id)
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
    if payload["kind"] in TRACKER_OPERATIONS:
        return
    if payload["kind"] == "catch_up" and payload["outcome"] == "clean":
        ticket.caught_up = payload["before"]
    elif payload["kind"] == "file_issues":
        if payload["outcome"] in ("refused", "failed"):
            ticket.artifacts.append(payload["artifact"])
    elif payload["kind"] == "catch_up":
        ticket.conflict = Conflict(payload["before"], payload["files"])
        ticket.artifacts.append(payload["artifact"])
    elif payload["outcome"] == "merged":
        ticket.state, ticket.merged_commit = TicketState.MERGED, payload["after"]
        memory.landings.append(Landing(ticket.id, payload["before"], payload["after"]))


@applies(Event.TrackerActed)
def _apply_tracker_acted(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.acted.setdefault(payload.get("ticket", RUN_NODE_ACTS), set()).add((payload["target"], payload["action"]))


@applies(Event.TrackerActionFailed)
def _apply_tracker_action_failed(memory: RunMemory, payload: dict[str, Any]) -> None:
    if payload["on_fail"] == WARN:
        _apply_tracker_acted(memory, payload)


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
        entry.details = [", ".join(f"#{issue_id(issue)}" for issue in payload["issues"])]


@measures(Event.OperationFinished)
def _measure_operation_finished(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is None:
        return
    ticket.work_s += payload.get("duration") or 0
    if payload["outcome"] == "merged" and "files_changed" in payload:
        ticket.merge_change = size(payload["node"], payload)
