"""The Decisions of the Run are the questions the owner answers for the whole Run, not for one Ticket.

`infra_failure` opens when Attempts of two Tickets fail in a row with an infrastructure cause, and offers `retry` or
`abort`.
`ci_failed`, which the CI wait opens, offers `recheck` or `accept`.
"""

from collections.abc import Callable
from typing import Any, Protocol

from factory import deciders
from factory.artifacts import RunDir, write_once
from factory.attempt import gave_back
from factory.ci_wait import CI_OUTCOMES
from factory.commands import RUN_LEVEL, Decide
from factory.decision import Asking, check_asked, check_ruling
from factory.events import Event
from factory.journal import CommandRow, Journal
from factory.question import render_run_card
from factory.run_state import CI_DECISION, InfraFailure, RunMemory
from factory.run_view import RunView
from factory.states import Failure

# Attempts of two Tickets that fail in a row with one of these causes open a Decision of the Run.
RUN_DECISION = "infra_failure"
RUN_OUTCOMES = {
    "retry": "Start the Attempts again; the failed ones spent no Round or attempt",
    "abort": "End the Run as aborted",
}


class Scope(Protocol):
    """A Scope is the Run that asks: its id, its directory, the state its events tell and what an answer sets."""

    run_id: str
    run_dir: RunDir
    memory: RunMemory
    ci_note: str
    aborting: str | None

    @property
    def failures(self) -> list[InfraFailure]: ...


class RunDecisions:
    """RunDecisions opens the Decisions of the Run and takes the owner's answers to them.

    `append` journals an event and applies it to the Run state.
    """

    def __init__(
        self,
        journal: Journal,
        append: Callable[[Event, dict[str, Any]], None],
        view: RunView,
        scope: Scope,
    ) -> None:
        self.journal = journal
        self.append = append
        self.view = view
        self.scope = scope

    def ask(self, cause: Failure) -> None:
        """Opens the Run's Decision `retry | abort` and gives back what the Attempts of the row charged.

        The Attempt module says what there is to give back, and the give-back is part of the event,
        so replay and `factory status` see it.
        """
        round = self.scope.memory.run_asked + 1
        names = [f"Ticket {f.ticket} at {f.node}, Round {f.round}" for f in self.scope.failures]
        card = write_once(
            self.scope.run_dir.root / "decisions" / f"{RUN_DECISION}-{round}.md",
            render_run_card(self.scope.run_id, RUN_DECISION, cause, names, RUN_OUTCOMES),
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
                "tickets": sorted({f.ticket for f in self.scope.failures}),
                "gave_back": gave_back(self.scope.memory),
            },
        )
        self.view.ring()

    def answer(self, command: CommandRow, answer: Decide) -> None:
        """Accepts the answer to the Run's Decision.

        For `infra_failure`, `retry` goes on and `abort` ends the Run after the tick.
        For `ci_failed`, `recheck` waits for the CI again and `accept` ends the Run with the red CI recorded.
        """
        check_asked(
            Asking("the Run", self.scope.memory.run_waiting, bool(self.scope.memory.run_asked)),
            answer.node,
            answer.round,
        )
        assert self.scope.memory.run_waiting is not None
        decision = self.scope.memory.run_waiting[0]
        outcomes = CI_OUTCOMES if decision == CI_DECISION else RUN_OUTCOMES
        outcome = check_ruling(answer.outcome, answer.reasons, tuple(outcomes), lambda _: False).outcome
        if decision == CI_DECISION and outcome == "accept":
            ended = "timed out" if self.scope.memory.ci and self.scope.memory.ci.outcome == "timeout" else "failed"
            self.scope.ci_note = f"CI {ended}, accepted by the owner"
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
        if outcome == "abort":
            self.scope.aborting = "the owner aborted the Run after Attempts failed in a row"
