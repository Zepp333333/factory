"""The Run state is what the Orchestrator keeps in memory about a Run, and `apply` is the one place that changes it.

`apply` folds an event through the handler that the event's writer registered in `events.applies`, so this module
names no event.

Recording an event appends it to the journal and applies it here, and `recovery.replay` folds the journal
through the same `apply`, so a resumed Run starts from the state the dead process held.
An event carries everything `apply` needs, such as what an Attempt charged, what the Run's Decision gave back
and the Artifact an event wrote, so `apply` holds no rule that the live code decides on its own.
"""

from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from factory.core.events import applies
from factory.core.states import OOM, Failure, TicketState
from factory.pipeline import gates
from factory.pipeline.pipeline import Pipeline, Stage
from factory.pipeline.throttle import WHOLE_RUN, Brake, Braked

# Attempts of two Tickets that fail in a row with one of these causes open a Decision of the Run.
# Two Attempts of one Ticket that fail in a row with `oom` open it too, because the same memory fails again.
INFRA_FAILURES: tuple[Failure, ...] = ("auth", "sandbox_start", "executor_crash", "timeout", "stalled", "oom")


# The Run's Decision that the Run opens when the CI of its final PR is red or does not end in time.
CI_DECISION = "ci_failed"
# The Decision `ci_failed` offers these outcomes.
CI_OUTCOMES = {
    "recheck": "Wait for the CI of the PR's current head again, after you fixed or reran something",
    "accept": "End the Run as it is, with the red CI recorded",
}
# The Run's Decision that the Run opens when an Attempt waits and a budget is spent.
BUDGET_DECISION = "budget_spent"
# A CI wait that ends with one of these outcomes needs no Decision.
CI_ENDS = ("passed", "unchecked")


@dataclass(frozen=True)
class Conflict:
    """A Conflict is the merge of `tip` that `sync` left for `resolve` to edit and the host to commit."""

    tip: str
    files: list[str]


@dataclass(frozen=True)
class PassedProbe:
    """A PassedProbe is the Probe `probe` of Gate `gate` that passed with `verdict`, for the Gates that reuse it."""

    gate: str
    probe: int
    verdict: str


@dataclass(frozen=True)
class Landing:
    """A Landing is one `merge` of a Ticket, which moved the Integration branch from `before` to `after`."""

    ticket: str
    before: str
    after: str


@dataclass(frozen=True)
class InfraFailure:
    """An InfraFailure is a failed Attempt of an infrastructure cause class, in the Round of `node` it failed in.

    `charge` is the counter the Attempt spent, or None for one the Run's Decision held.
    """

    ticket: str
    cause: Failure
    node: str | None
    round: int
    charge: str | None = None


@dataclass(frozen=True)
class CiWait:
    """A CiWait is the Run's wait for the CI of `commit` on its final PR, which began at `started`.

    `outcome` is empty while the CI goes, and `failed` or `timeout` once it ended red or ran out of time,
    when the Run's Decision `ci_failed` decides how the Run goes on.
    """

    commit: str
    started: datetime
    outcome: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.outcome)


@dataclass
class TicketRuntime:
    """A TicketRuntime holds what the Orchestrator keeps in memory about a Ticket between ticks.

    Only `apply` changes the fields that events tell.
    `rounds` numbers each Stage's Rounds over the whole Ticket, and `rounds_since_reset` counts them for
    the round limit and model escalation since `reset_rounds` last started the count over.
    `artifacts` lists what the Ticket's Attempts and Probes left, in order, and `cause` is the one whose
    outcome led to the current node.
    `probes` numbers each Gate's Probes over the whole Ticket, and `gate_attempt` counts them since the Ticket
    last entered its current Gate.
    `probe_results` keeps each Gate's latest finished Probe with its number, for the Gates that read it with `from`.
    `passed` maps the kind, the resolved `with` and the git tree of each Probe that passed to that Probe, for the
    Gates that reuse a pass.
    `spent` counts the Attempts of the current Round that count toward the Stage's `attempts`.
    `sandbox_losses` counts the Attempts and Probes of the whole Ticket that its Sandbox lost.
    `conflict` is the merge `sync` left in the Workspace until the host commits it after `resolve`.
    `synced` is the Integration branch tip the Ticket branch last took in, `base` at claim and then each `sync`'s.
    The Brief and `min_changed` diff from it, so they see only the work of this Ticket.
    `entered_at` is when the Ticket entered its current node, which the duration of a Gate, Operation or Decision
    counts from.
    `issues` lists the issues `file_issues` filed or reused for the Ticket, in order, each with its key and number.
    `waiting` is the node and Round of the human Decision the Ticket waits at, and `answered` tells that a human
    has decided a Decision of the Ticket.
    `waiting_head` is the head of the Ticket branch when the Ticket began to wait, which the owner's commits build on.
    """

    id: str
    blocked_by: list[str]
    state: TicketState = TicketState.QUEUED
    node: str | None = None
    round: int = 0
    attempt: int = 0
    spent: int = 0
    sandbox_losses: int = 0
    rounds: dict[str, int] = field(default_factory=dict)
    rounds_since_reset: dict[str, int] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    cause: dict[str, Any] | None = None
    branch: str = ""
    base: str = ""
    merged_commit: str = ""
    kept: bool = False
    workspace: Path | None = None
    probes: dict[str, int] = field(default_factory=dict)
    probe_results: dict[str, tuple[int, gates.ProbeResult]] = field(default_factory=dict)
    passed: dict[str, PassedProbe] = field(default_factory=dict)
    gate_attempt: int = 0
    conflict: Conflict | None = None
    synced: str = ""
    entered_at: datetime | None = None
    waiting: tuple[str, int] | None = None
    waiting_head: str = ""
    answered: bool = False
    issues: list[dict[str, Any]] = field(default_factory=list)

    def seconds_in_node(self, now: datetime) -> float | None:
        """Returns how long the Ticket has been in its current node, or None when the journal does not say."""
        return (now - self.entered_at).total_seconds() if self.entered_at else None


@dataclass
class RunMemory:
    """A RunMemory is the state of a Run that its events tell: its Tickets and the parts of the Run outside them.

    `in_flight` and `probing` hold the `AttemptStarted` and `ProbeStarted` payloads of the Tickets whose
    Attempt or Probe has no end event.
    `failures` is the row of Attempts that failed in a row with an infrastructure cause, which opens the Run's Decision.
    `run_waiting` is that Decision while it waits, and `run_asked` counts how often it opened.
    `max_parallel`, `round_limits` and `throttle` are what `limits` last set, and `throttle` starts as the project's.
    `braked` holds, by the name of its Executor, the window that holds back the new Attempts on that Executor while
    the Run spends it too fast.
    `budgets` are the dollars the Run may spend, which `limits` may have changed since the project's, and `spent` holds
    the cost of the Attempts that ended by the name of their Executor, which `spending` holds for each Attempt's slot.
    `finishing` tells that the owner ended the Run at its budget, so its unfinished Tickets are abandoned.
    `landings` lists each merge, and `holder` and `queue` are the Merge window's.
    `pull_request` is the URL of the Run's final PR once it is open, or its path in the folder the PR is recorded in,
    and `pushed` is the commit the Run pushed its final branch at.
    A stacked Run's final PR is that of its top layer, and `opened` lists the heads of the PRs open or recorded.
    `ci` is the wait for the CI of that PR until it ends, and `ci_ended` says how it ended: `passed`, `absent`,
    `unchecked`, or the `failed` or `timeout` the owner accepted.
    `ci_fixes` lists the Rounds of the CI fixer, each with the failed jobs it started on and, once it ended, its report.
    `aborted` tells that the owner answered the Run's Decision with `abort`.
    `pipeline` is the Run's Pipeline, which `apply` hands the handlers with the state, and no event tells.
    """

    tickets: list[TicketRuntime]
    max_parallel: int
    landings: list[Landing] = field(default_factory=list)
    holder: str | None = None
    queue: list[str] = field(default_factory=list)
    paused_until: datetime | None = None
    failures: list[InfraFailure] = field(default_factory=list)
    run_waiting: tuple[str, int] | None = None
    run_asked: int = 0
    in_flight: dict[str, dict[str, Any]] = field(default_factory=dict)
    probing: dict[str, dict[str, Any]] = field(default_factory=dict)
    round_limits: dict[str, int] = field(default_factory=dict)
    throttle: dict[str, Brake] = field(default_factory=dict)
    braked: dict[str, Braked] = field(default_factory=dict)
    budgets: dict[str, float] = field(default_factory=dict)
    spent: dict[str, float] = field(default_factory=dict)
    spending: dict[str, str] = field(default_factory=dict)
    finishing: bool = False
    pull_request: str = ""
    pushed: str = ""
    opened: list[str] = field(default_factory=list)
    ci: CiWait | None = None
    ci_ended: str = ""
    ci_fixes: list[dict[str, Any]] = field(default_factory=list)
    aborted: bool = False
    pipeline: Pipeline | None = field(default=None, repr=False, compare=False)

    def ticket(self, id: str) -> TicketRuntime | None:
        return next((t for t in self.tickets if t.id == id), None)

    def ticket_of(self, payload: dict[str, Any]) -> TicketRuntime | None:
        """Returns the Ticket an event's payload names, or None for an event of the Run or of no Ticket of it."""
        return self.ticket(payload.get("ticket", ""))

    def commits(self, node: str) -> bool:
        """Tells whether `node` of the Run's Pipeline is a Stage that commits its Workspace."""
        found = self.pipeline.nodes[node] if self.pipeline is not None else None
        return isinstance(found, Stage) and found.commits

    def row_with(self, failure: InfraFailure) -> list[InfraFailure]:
        """Returns the row of failed Attempts once `failure` joins it, which another cause starts over."""
        if self.failures and self.failures[-1].cause != failure.cause:
            return [failure]
        return [*self.failures, failure]

    def opens_decision(self, ticket: str, cause: Failure, node: str | None, round: int) -> bool:
        """Tells whether an Attempt of `ticket` that failed with `cause` is held by the Run's Decision.

        A held Attempt spends nothing.

        Attempts of two different Tickets that fail in a row with one infrastructure cause open that Decision,
        and so do two Attempts of one Ticket that fail in a row with `oom`.
        Attempts that fail while it waits join it.
        """
        if self.run_waiting is not None:
            return True
        row = self.row_with(InfraFailure(ticket, cause, node, round))
        return len({f.ticket for f in row}) >= 2 or (cause == OOM and len(row) >= 2)

    def spent_of(self, name: str) -> float:
        """Returns the dollars spent against the budget `name`, of the whole Run or of an Executor."""
        return sum(self.spent.values()) if name == WHOLE_RUN else self.spent.get(name, 0)

    def over_budget(self) -> list[str]:
        """Returns the keys of the budgets that are spent, which a cost right at the cap is."""
        return [name for name, budget in self.budgets.items() if self.spent_of(name) >= budget]

    def comparable(self) -> dict[str, Any]:
        """Returns the state that events tell, for a check that replaying them gives the live state."""
        told = [f.name for f in fields(TicketRuntime)]
        tickets = {t.id: {name: getattr(t, name) for name in told} for t in self.tickets}
        told_run = [f.name for f in fields(self) if f.name not in ("tickets", "pipeline")]
        return {**{name: getattr(self, name) for name in told_run}, "tickets": tickets}


def apply(pipeline: Pipeline, memory: RunMemory, type: str, payload: dict[str, Any]) -> None:
    """Applies one event to the Run state through the handler its writer registered in `applies`.

    An event with no handler, such as one of a newer factory, leaves the state as it is.
    """
    memory.pipeline = pipeline
    handler = applies.get(type)
    if handler is not None:
        handler(memory, payload)
