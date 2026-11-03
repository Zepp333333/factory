"""The Decisions of the Run are the questions the owner answers for the whole Run, not for one Ticket.

`infra_failure` opens when Attempts of two Tickets fail in a row with an infrastructure cause, and offers `retry` or
`abort`.
`ci_failed`, which the CI wait opens, offers `recheck` or `accept`.
"""

from typing import Any

from factory.core.events import Event
from factory.core.states import Failure
from factory.journal.commands import RUN_LEVEL, Decide
from factory.journal.journal import CommandRow, Journal
from factory.journal.question import render_run_card
from factory.journal.run_state import CI_DECISION, CI_OUTCOMES, RunMemory
from factory.pipeline import deciders
from factory.run.decision import Asking, check_asked, check_ruling
from factory.run.run_context import Append, RunContext
from factory.views.artifacts import write_once
from factory.views.run_view import RunView

# Attempts of two Tickets that fail in a row with one of these causes open a Decision of the Run.
RUN_DECISION = "infra_failure"
RUN_OUTCOMES = {
    "retry": "Start the Attempts again; the failed ones spent no Round or attempt",
    "abort": "End the Run as aborted",
}


def gave_back(memory: RunMemory) -> list[dict[str, Any]]:
    """Returns what the Run's Decision gives back of the Attempts in the row of failures.

    Only the Attempts whose Ticket is still in the same Round have anything to give back.
    """
    given = []
    for failed in memory.failures:
        ticket = memory.ticket(failed.ticket)
        assert ticket is not None
        if failed.charge is not None and (ticket.node, ticket.round) == (failed.node, failed.round):
            given.append({"ticket": failed.ticket, "counter": failed.charge})
    return given


class RunDecisions:
    """RunDecisions opens the Decisions of the Run and takes the owner's answers to them."""

    def __init__(self, context: RunContext, journal: Journal, append: Append, view: RunView) -> None:
        self.journal = journal
        self.append = append
        self.view = view
        self.context = context

    def ask(self, cause: Failure) -> None:
        """Opens the Run's Decision `retry | abort` and gives back what the Attempts of the row charged.

        `gave_back` says what there is to give back, and the give-back is part of the event,
        so replay and `factory status` see it.
        """
        round = self.context.memory.run_asked + 1
        names = [f"Ticket {f.ticket} at {f.node}, Round {f.round}" for f in self.context.memory.failures]
        card = write_once(
            self.context.run_dir.root / "decisions" / f"{RUN_DECISION}-{round}.md",
            render_run_card(self.context.run_id, RUN_DECISION, cause, names, RUN_OUTCOMES),
        )
        self.append(
            Event.DecisionWaiting,
            {
                "ticket": RUN_LEVEL,
                "node": RUN_DECISION,
                "round": round,
                "card": str(card),
                "outcomes": list(RUN_OUTCOMES),
                "cause": cause,
                "tickets": sorted({f.ticket for f in self.context.memory.failures}),
                "gave_back": gave_back(self.context.memory),
            },
        )
        self.view.ring()

    def answer(self, command: CommandRow, answer: Decide) -> None:
        """Accepts the answer to the Run's Decision.

        For `infra_failure`, `retry` goes on and `abort` ends the Run after the tick.
        For `ci_failed`, `recheck` waits for the CI again and `accept` ends the Run with the red CI recorded.
        """
        check_asked(
            Asking("the Run", self.context.memory.run_waiting, bool(self.context.memory.run_asked)),
            answer.node,
            answer.round,
        )
        assert self.context.memory.run_waiting is not None
        decision = self.context.memory.run_waiting[0]
        outcomes = CI_OUTCOMES if decision == CI_DECISION else RUN_OUTCOMES
        outcome = check_ruling(answer.outcome, answer.reasons, tuple(outcomes), lambda _: False).outcome
        with self.journal.transaction():
            self.append(
                Event.CommandAccepted,
                {
                    "command": command.id,
                    "kind": command.kind,
                    "ticket": RUN_LEVEL,
                    "result": f"Run decided {decision}: {outcome}",
                },
            )
            self.append(
                Event.DecisionDecided,
                {
                    "ticket": RUN_LEVEL,
                    "node": decision,
                    "round": answer.round,
                    "decider": deciders.OWNER,
                    "outcome": outcome,
                    "reasons": answer.reasons,
                },
            )
