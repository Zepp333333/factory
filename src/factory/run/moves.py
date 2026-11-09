"""Moves take a Ticket from node to node: its claim, the routes of its outcomes and the start of each Round.

The Orchestrator, Probes, RunStart and the inbox all move Tickets, and each moves them here, so none of them calls
back into another to route an outcome.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from factory.config import Project
from factory.core.events import Event, applies, measures, nothing, times
from factory.core.git import HostGit
from factory.core.names import sandbox_name, ticket_branch
from factory.core.states import TERMINAL_NODES, TicketState
from factory.journal.journal import Journal
from factory.journal.metrics_memory import MetricsMemory
from factory.journal.run_state import RunMemory
from factory.journal.timeline_memory import TimelineMemory
from factory.pipeline.pipeline import Decision, Node, Stage
from factory.ports.run_ports import Ports
from factory.ports.workbench import WorkbenchError
from factory.run.attempt import Attempts
from factory.run.decision import Decisions
from factory.run.live import Ended, LiveTicket
from factory.run.merge_window import MergeWindow
from factory.run.operations import Operations
from factory.run.run_context import Append, RunContext


class Moves:
    """Moves claims the Tickets of a Run and takes each along the routes of its Pipeline."""

    def __init__(
        self,
        context: RunContext,
        journal: Journal,
        append: Append,
        project: Project,
        ports: Ports,
        home: Path,
        git: HostGit,
        window: MergeWindow,
        attempts: Attempts,
        decisions: Decisions,
        operations: Operations,
    ) -> None:
        self.context = context
        self.journal = journal
        self.append = append
        self.project = project
        self.pipeline = project.pipeline
        self.ports = ports
        self.home = home
        self.git = git
        self.window = window
        self.attempts = attempts
        self.decisions = decisions
        self.operations = operations

    def claim(self, ticket: LiveTicket) -> bool:
        """Claims the Ticket and starts its first Attempt, and returns False when no slot is free.

        The slot, the claim and the record of the Attempt are one transaction of the Journal, so no other Run
        can take the slot in between.
        The Workspace starts from the Integration branch's tip as it is now, and the Sandbox is created after the claim.
        A Pipeline that starts at a Gate or an Operation needs no slot to claim a Ticket.
        """
        run_id = self.context.run_id
        start = self.pipeline.nodes[self.pipeline.start]
        stage = start if isinstance(start, Stage) else None
        base = self.git.resolve(self.context.integration)
        with self.journal.transaction():
            if stage is not None and not self.attempts.take_slot(ticket.id):
                return False
            workspace = self.home / "workspaces" / run_id / ticket.id
            self.append(
                Event.TicketClaimed,
                {
                    "ticket": ticket.id,
                    "branch": ticket_branch(run_id, ticket.id),
                    "base": base,
                    "workspace": str(workspace),
                },
            )
            self.enter(ticket, self.pipeline.start, None)
            request = self.attempts.begin(ticket, stage, base) if stage is not None else None
        assert ticket.workspace is not None
        self.git.create_branch(ticket.branch, ticket.base)
        self.ports.workbench.create_workspace(self.project.repo, ticket.workspace, ticket.branch)
        mounts = self.attempts.isolation.mounts(ticket, self.context.run_dir)
        self.ports.sandbox.create(sandbox_name(run_id, ticket.id), ticket.workspace, mounts)
        if request is not None:
            self.attempts.launch(ticket, request, base)
        return True

    def lose_attempt(
        self,
        ticket: LiveTicket,
        attempt: dict[str, Any],
        cause: str,
        reason: str,
        mended: list[str],
        extra: dict[str, Any],
    ) -> None:
        """Loses the Attempt `attempt` and ends the node if that was one loss too many for the Ticket's Sandbox."""
        ended = self.attempts.lose(ticket, attempt, cause, reason, mended, extra)
        if ended is not None:
            self.conclude(ticket, ended)

    def conclude(self, ticket: LiveTicket, ended: Ended) -> None:
        """Ends the node the Ticket is at after every Attempt of its Round failed: a Stage gives `failed`,
        and a model Decider escalates.
        """
        assert ticket.node is not None
        node = self.pipeline.nodes[ticket.node]
        if isinstance(node, Decision):
            outcome, artifact = self.decisions.record(ticket, node, self.decisions.model_ruling(ended))
            self.leave(ticket, node, outcome, artifact)
        else:
            self.leave(ticket, node, "failed", None)

    def leave(self, ticket: LiveTicket, node: Node, outcome: str, cause: dict[str, Any] | None) -> None:
        """Takes the route of `outcome` with `cause` for the next node.

        The route first starts the round count of the Stages it resets over.
        """
        resets = node.resets.get(outcome, ())
        if resets:
            self.append(Event.RoundsReset, {"ticket": ticket.id, "node": node.name, "stages": list(resets)})
        self.enter(ticket, self.pipeline.route(node, outcome), cause)

    def enter(self, ticket: LiveTicket, target: str, cause: dict[str, Any] | None) -> None:
        """Enters `target`, and starts a Round when it is a Stage or a Decision.

        `cause` is the artifact whose outcome led here.
        A Stage past its round limit is not entered and gives `exhausted`, which keeps the cause.
        """
        node = self.pipeline.nodes.get(target)
        if node is None or not node.HOLDS_WINDOW:
            self.window.release(ticket.id, ticket.node)
        payload: dict[str, Any] = {"ticket": ticket.id, "node": target, "at": self.ports.clock.now().isoformat()}
        if cause is not None:
            payload["cause"] = cause
        if isinstance(node, Stage | Decision):
            count = ticket.rounds_since_reset.get(target, 0) + 1
            round_limits = self.context.memory.round_limits
            limit = round_limits.get(target, node.max_rounds) if isinstance(node, Stage) else None
            if limit is not None and count > limit:
                to = self.pipeline.route(node, "exhausted")
                self.append(
                    Event.RoundsExhausted,
                    {"ticket": ticket.id, "node": target, "rounds": limit, "outcome": "exhausted", "to": to},
                )
                self.leave(ticket, node, "exhausted", cause)
                return
            payload["round"] = ticket.rounds.get(target, 0) + 1
            payload["rounds_since_reset"] = count
        self.append(Event.NodeEntered, payload)
        if target in TERMINAL_NODES and ticket.state == TicketState.MERGED:
            self.operations.clean_up(ticket, ticket.merged_commit)
        if target == "abandoned" and ticket.state == TicketState.ABANDONED:
            self.operations.abandon(ticket)

    def mark_stage(self, ticket: LiveTicket) -> None:
        """Shows the Ticket's node and Round on the card of its Workspace once they change.

        A Workbench that cannot show it is not a reason to stop the Ticket.
        """
        node = self.pipeline.nodes.get(ticket.node or "")
        if ticket.workspace is None or node is None or not ticket.workspace.exists():
            return
        text = f"{ticket.node}, Round {ticket.round}" if isinstance(node, Stage | Decision) else str(ticket.node)
        if text == ticket.marked:
            return
        try:
            self.ports.workbench.mark_stage(ticket.workspace, text)
        except (WorkbenchError, OSError):
            return
        ticket.marked = text


# What the moves of a Ticket tell the readers of the Journal.

applies(Event.RoundsExhausted)(nothing)


@applies(Event.TicketClaimed)
def _apply_ticket_claimed(memory: RunMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is not None:
        ticket.branch, ticket.base, ticket.caught_up = payload["branch"], payload["base"], payload["base"]
        ticket.workspace = Path(payload["workspace"])
        ticket.state = TicketState.ACTIVE


@applies(Event.NodeEntered)
def _apply_node_entered(memory: RunMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is None:
        return
    ticket.node, ticket.gate_attempt = payload["node"], 0
    memory.acted.pop(ticket.id, None)
    if "at" in payload:
        ticket.entered_at = datetime.fromisoformat(payload["at"])
    ticket.cause = payload.get("cause")
    if "round" in payload:
        ticket.round, ticket.attempt, ticket.spent = payload["round"], 0, 0
        ticket.rounds[payload["node"]] = payload["round"]
        ticket.rounds_since_reset[payload["node"]] = payload["rounds_since_reset"]
    if payload["node"] in TERMINAL_NODES and ticket.state in (TicketState.QUEUED, TicketState.ACTIVE):
        ticket.state = TicketState(payload["node"])


@applies(Event.RoundsReset)
def _apply_rounds_reset(memory: RunMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is not None:
        for stage in payload["stages"]:
            ticket.rounds_since_reset[stage] = 0


@times(Event.NodeEntered)
def _time_node_entered(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    ticket = payload.get("ticket", "")
    memory.enter(ticket, ticket, payload["node"])


@measures(Event.TicketClaimed)
def _measure_ticket_claimed(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is None:
        return
    at, begin = memory.at, memory.begin
    memory.claimed[ticket.ticket] = at
    memory.active.add(ticket.ticket)
    memory.settle(at)
    blockers = (memory.blockers or {}).get(ticket.ticket, [])
    ready = max([memory.done_at[b] for b in blockers if b in memory.done_at], default=begin)
    ready = min(max(ready, begin), at)
    ticket.wait_blockers_s = (ready - begin).total_seconds()
    memory.gaps.setdefault(ticket.ticket, []).append((ready, at))


@measures(Event.NodeEntered)
def _measure_node_entered(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is None:
        return
    node, at = payload["node"], memory.at
    if node in TERMINAL_NODES:
        ticket.outcome = "merged" if node == "done" else "abandoned"
        memory.done_at[ticket.ticket] = at
        claimed = memory.claimed.get(ticket.ticket)
        ticket.duration_s = (at - claimed).total_seconds() if claimed is not None else 0
        memory.active.discard(ticket.ticket)
        memory.settle(at)
        memory.idle.pop(ticket.ticket, None)
    elif "round" in payload:
        ticket.rounds[node] = max(ticket.rounds.get(node, 0), payload["round"])
        memory.idle[ticket.ticket] = (node, at)
        if (payload.get("cause") or {}).get("kind") == "gate":
            ticket.fix_rounds += 1
    else:
        memory.idle.pop(ticket.ticket, None)
