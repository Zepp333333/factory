"""The inbox of a Run holds the owner's Commands, and this phase accepts or rejects each one and carries it out.

`cancel` abandons a Ticket, `override` replaces the outcome of a Decision, `limits` changes the Run's parallelism,
round limits, brakes and budgets, and `decide` answers a Decision of a Ticket or, through RunDecisions, of the Run.
"""

import json
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from factory.core.events import Event, applies, measures, nothing
from factory.core.git import HostGit
from factory.core.states import TERMINAL_NODES, TicketState
from factory.journal.commands import RUN_LEVEL, BadCommand, Cancel, Decide, Limits, parse
from factory.journal.commands import Override as OverrideCommand
from factory.journal.journal import CommandRow, Journal
from factory.journal.metrics_memory import MetricsMemory
from factory.journal.run_state import RunMemory
from factory.pipeline import deciders
from factory.pipeline.pipeline import Decision, Pipeline
from factory.pipeline.throttle import WHOLE_RUN, Brake
from factory.run.attempt import Attempts
from factory.run.decision import (
    Asking,
    Decisions,
    Override,
    Rejected,
    brake_key,
    check_asked,
    check_limits,
    check_ruling,
)
from factory.run.live import LiveTicket, attempt_key
from factory.run.merge_window import MergeWindow
from factory.run.moves import Moves
from factory.run.run_context import Append, RunContext
from factory.run.run_decision import RunDecisions


def _shares(shares: dict[str, float]) -> str:
    return ", ".join(f"{name} {share}" for name, share in shares.items())


class Inbox:
    """Inbox carries out the Commands that wait in the inbox of a Run, oldest first."""

    def __init__(
        self,
        context: RunContext,
        journal: Journal,
        append: Append,
        pipeline: Pipeline,
        git: HostGit,
        window: MergeWindow,
        attempts: Attempts,
        run_decisions: RunDecisions,
        decisions: Decisions,
        moves: Moves,
    ) -> None:
        self.context = context
        self.journal = journal
        self.append = append
        self.pipeline = pipeline
        self.git = git
        self.window = window
        self.attempts = attempts
        self.decisions = decisions
        self.run_decisions = run_decisions
        self.moves = moves

    def apply(self) -> bool:
        """Accepts or rejects each Command in the inbox of the Run, oldest first, and returns whether any came."""
        commands = self.journal.pending_commands(self.context.run_id)
        for command in commands:
            try:
                match parse(command):
                    case Decide() as answer if answer.ticket == RUN_LEVEL:
                        self.run_decisions.answer(command, answer)
                    case Decide() as answer:
                        ticket, decision, ruling = self._check_answer(answer)
                        self._carry_out_answer(command, ticket, decision, ruling)
                    case Cancel() as cancel:
                        self._cancel(command, cancel)
                    case OverrideCommand() as override:
                        self._override(command, override)
                    case Limits() as limits:
                        self._set_limits(command, limits)
            except (Rejected, BadCommand) as rejection:
                self.append(
                    Event.CommandRejected, {"command": command.id, "kind": command.kind, "reason": str(rejection)}
                )
        return bool(commands)

    def _ticket_of(self, ticket_id: str) -> LiveTicket:
        ticket = next((t for t in self.context.tickets if t.id == ticket_id), None)
        if ticket is None:
            raise Rejected(f"no Ticket {ticket_id} in the Run")
        return ticket

    def _accept(self, command: CommandRow, ticket: str, result: str) -> None:
        self.append(
            Event.CommandAccepted, {"command": command.id, "kind": command.kind, "ticket": ticket, "result": result}
        )

    def _cancel(self, command: CommandRow, cancel: Cancel) -> None:
        """Abandons a Ticket that is queued or at work: its Attempt or Probe is stopped and its work is saved.

        A Ticket that depends on it never starts, since it only starts once all its blockers are merged.
        """
        ticket = self._ticket_of(cancel.ticket)
        if ticket.state not in (TicketState.QUEUED, TicketState.ACTIVE):
            raise Rejected(f"Ticket {ticket.id} is already {ticket.state}")
        self._abandon(ticket, "the owner cancelled the Ticket", command)

    def abandon_unfinished(self) -> bool:
        """Abandons every Ticket that did not merge, for a Run the owner finished, and returns whether there was any."""
        unfinished = [t for t in self.context.tickets if t.state in (TicketState.QUEUED, TicketState.ACTIVE)]
        for ticket in unfinished:
            self._abandon(ticket, "the owner finished the Run at its budget")
        return bool(unfinished)

    def _abandon(self, ticket: LiveTicket, reason: str, command: CommandRow | None = None) -> None:
        """Stops the work of a Ticket and abandons it, which accepts the `command` that asked for it, if any did."""
        queued = ticket.state == TicketState.QUEUED
        if not queued:
            self._stop_work(ticket, "cancelled", reason)
        with self.journal.transaction():
            if command is not None:
                self._accept(command, ticket.id, f"Ticket {ticket.id} cancelled, abandoned")
            self.append(Event.TicketCancelled, {"ticket": ticket.id, "node": ticket.node})
        self.window.forget(ticket.id)
        if not queued:
            self.moves.enter(ticket, "abandoned", ticket.cause)

    def _stop_work(self, ticket: LiveTicket, cause: str, reason: str) -> None:
        """Stops the Attempt or Probe the Ticket has going and puts the Workspace back where it was committed.

        The Attempt is lost with `cause`, which spends none of the Stage's and no Round, and a merge that `sync`
        left in the Workspace is dropped.
        A Ticket that waits for the owner keeps the owner's edits in the Workspace.
        """
        assert ticket.workspace is not None
        running, ticket.running = ticket.running, None
        probe, ticket.probe = ticket.probe, None
        merging = ticket.conflict is not None
        if merging:
            self.append(Event.ConflictDropped, {"ticket": ticket.id})
        if probe is not None:
            probe.process.kill()
            self.append(Event.ProbeInterrupted, {"ticket": ticket.id, "node": ticket.node, "reason": reason})
        if running is not None:
            self.attempts.kill(running)
            key = {**attempt_key(ticket), "head_before": running.head_before}
            discarded = self.attempts.discard(ticket, running.head_before, running.request.attempt_dir)
            self.moves.lose_attempt(ticket, key, cause, reason, [], discarded)
        elif probe is not None or merging:
            self.git.reset(ticket.workspace, ticket.branch, ignored=False)

    def _override(self, command: CommandRow, override: OverrideCommand) -> None:
        """Replaces the outcome of the Decision the Ticket is at, or else its last one, and takes the new route.

        What the Ticket was doing since that Decision is stopped, like the work of a cancelled Ticket.
        The new outcome gets an Artifact of its own next to the old one, which stays.
        """
        ticket = self._ticket_of(override.ticket)
        if ticket.state != TicketState.ACTIVE or ticket.node is None:
            raise Rejected(
                f"Ticket {ticket.id} is {ticket.state}, and only a Ticket at work has a Decision to override"
            )
        node = self.pipeline.nodes[ticket.node]
        previous: dict[str, Any] | None = None
        if isinstance(node, Decision):
            decision, round = node, ticket.round
        else:
            previous = next((a for a in reversed(ticket.artifacts) if a["kind"] == "decision"), None)
            if previous is None:
                raise Rejected(f"Ticket {ticket.id} has made no Decision to override")
            decision, round = self.pipeline.nodes[previous["node"]], previous["round"]  # type: ignore[assignment]
        assert isinstance(decision, Decision)
        ruling = check_ruling(
            override.outcome,
            override.reasons,
            decision.outcomes,
            partial(self.pipeline.sends_back_to_work, decision),
        )
        outcome = ruling.outcome
        waited = self.decisions.take_owner_edits(ticket) if ticket.waiting is not None else None
        self._stop_work(ticket, "overridden", f"the owner overrode the Decision {decision.name}")
        replacement = Override(round)
        if previous is not None:
            earlier = sum(
                a["kind"] == "decision" and (a["node"], a["round"]) == (decision.name, round) for a in ticket.artifacts
            )
            replacement = Override(
                round,
                self.context.run_dir.decision(ticket.id, decision.name, round).with_name(f"override-{earlier}.json"),
                json.loads(Path(previous["path"]).read_text())["outcome"],
            )
        target = self.pipeline.route(decision, outcome)
        restart = ticket.waiting is not None and target not in TERMINAL_NODES
        with self.journal.transaction():
            self._accept(
                command, ticket.id, f"Ticket {ticket.id} overrode {decision.name}: {outcome}, goes to {target}"
            )
            _, artifact = self.decisions.record(ticket, decision, ruling, waited, replacement)
        if restart:
            self.decisions.restart_sandbox(ticket)
        self.window.forget(ticket.id)
        self.moves.leave(ticket, decision, outcome, artifact)

    def _set_limits(self, command: CommandRow, limits: Limits) -> None:
        """Changes the Run's parallelism, its brakes and budgets and the round limits of its Stages, which count from
        the next Round."""
        check_limits(self.pipeline, limits)
        max_parallel, rounds = limits.max_parallel, limits.rounds
        throttle = {brake_key(window): shares for window, shares in limits.throttle.items()}
        with self.journal.transaction():
            changes = [] if max_parallel is None else [f"max_parallel {max_parallel}"]
            changes += [f"{name} rounds {limit}" for name, limit in rounds.items()]
            changes += [
                f"{window} brake lifted" if shares is None else f"{window} {_shares(shares)}"
                for window, shares in throttle.items()
            ]
            changes += [
                f"{'budget' if name == WHOLE_RUN else f'{name} budget'} ${budget:.2f}"
                for name, budget in limits.budgets.items()
            ]
            self._accept(command, "", f"Limits set: {', '.join(changes)}")
            self.append(
                Event.LimitsChanged,
                {"max_parallel": max_parallel, "rounds": rounds, "throttle": throttle, "budgets": limits.budgets},
            )

    def _check_answer(self, answer: Decide) -> tuple[LiveTicket, Decision, deciders.Ruling]:
        """Returns the Ticket, Decision and Ruling a `decide` Command asks for, or raises Rejected with the reason."""
        ticket = self._ticket_of(answer.ticket)
        asking = Asking(f"Ticket {ticket.id}", ticket.waiting, ticket.answered)
        check_asked(asking, answer.node, answer.round)
        assert ticket.waiting is not None and ticket.workspace is not None
        if not self.git.is_ancestor(ticket.waiting_head, self.git.head(ticket.workspace, ticket.branch)):
            raise Rejected("the history of the Workspace was rewritten while the Decision waited")
        decision = self.pipeline.nodes[ticket.waiting[0]]
        assert isinstance(decision, Decision)
        ruling = check_ruling(
            answer.outcome,
            answer.reasons,
            decision.outcomes,
            partial(self.pipeline.sends_back_to_work, decision),
        )
        return ticket, decision, ruling

    def _carry_out_answer(
        self, command: CommandRow, ticket: LiveTicket, decision: Decision, ruling: deciders.Ruling
    ) -> None:
        """Journals the accepted answer with the Decision's outcome in one transaction, then takes its route.

        The Sandbox starts again unless the route ends the Ticket.
        """
        waited = self.decisions.take_owner_edits(ticket)
        target = self.pipeline.route(decision, ruling.outcome)
        result = f"Ticket {ticket.id} decided {decision.name}: {ruling.outcome}, goes to {target}"
        with self.journal.transaction():
            self.append(
                Event.CommandAccepted,
                {"command": command.id, "kind": command.kind, "ticket": ticket.id, "result": result},
            )
            outcome, artifact = self.decisions.record(ticket, decision, ruling, waited)
        if target not in TERMINAL_NODES:
            self.decisions.restart_sandbox(ticket)
        self.moves.leave(ticket, decision, outcome, artifact)


# What the events of the inbox tell the readers of the Journal.

applies(Event.CommandAccepted, Event.CommandRejected)(nothing)


@applies(Event.ConflictDropped)
def _apply_conflict_dropped(memory: RunMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is not None:
        ticket.conflict = None


@applies(Event.TicketCancelled)
def _apply_ticket_cancelled(memory: RunMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is not None:
        ticket.waiting = None
        if ticket.state == TicketState.QUEUED:
            ticket.state = TicketState.ABANDONED


@applies(Event.LimitsChanged)
def _apply_limits_changed(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.max_parallel = payload["max_parallel"] or memory.max_parallel
    memory.round_limits.update(payload["rounds"])
    # A Journal from before the brakes named their Executor names a bare window.
    for window, shares in payload.get("throttle", {}).items():
        key = brake_key(window)
        if shares is None:
            memory.throttle.pop(key, None)
        else:
            memory.throttle[key] = replace(memory.throttle.get(key, Brake()), **shares)
    memory.budgets.update(payload.get("budgets", {}))


@measures(Event.LimitsChanged)
def _measure_limits_changed(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    memory.cap = payload.get("max_parallel") or memory.cap
    memory.settle(memory.at)
