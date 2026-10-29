"""The Run state is what the Orchestrator keeps in memory about a Run, and `apply` is the one place that changes it.

Recording an event appends it to the journal and applies it here, and `recovery.replay` folds the journal
through the same `apply`, so a resumed Run starts from the state the dead process held.
An event carries everything `apply` needs, such as what an Attempt charged, what the Run's Decision gave back
and the Artifact an event wrote, so `apply` holds no rule that the live code decides on its own.
"""

import json
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from factory import deciders, gates
from factory.artifacts import ProbeDir
from factory.commands import RUN_LEVEL
from factory.events import Event
from factory.executor import AttemptHandle, AttemptRequest, Failure
from factory.pipeline import TERMINAL_NODES, Pipeline, Stage
from factory.sandbox import Process
from factory.states import OOM, SANDBOX_LOSSES, SPENT, TicketState
from factory.throttle import Brake, Braked
from factory.workbench import AttemptTerminal

# Attempts of two Tickets that fail in a row with one of these causes open a Decision of the Run.
# Two Attempts of one Ticket that fail in a row with `oom` open it too, because the same memory fails again.
INFRA_FAILURES: tuple[Failure, ...] = ("auth", "sandbox_start", "executor_crash", "timeout", "stalled", "oom")


@dataclass(frozen=True)
class Conflict:
    """A Conflict is the merge of `tip` that `sync` left for `resolve` to edit and the host to commit."""

    tip: str
    files: list[str]


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


@dataclass
class Silence:
    """Silence tracks since when an Attempt's agent has made no progress, by what its `stream.ndjson` gains.

    Progress is a new agent message or a tool result, which the stream tells as an `assistant` or a `user` event.
    The Claude CLI's heartbeat about a long command is a `system` event, and it keeps coming when the command hangs,
    so it is not progress.
    """

    stream: Path
    since: datetime
    offset: int = 0

    def watch(self, now: datetime) -> float:
        """Reads what the stream gained since the last call, and returns the seconds since the last progress."""
        try:
            with self.stream.open("rb") as file:
                file.seek(self.offset)
                gained = file.read()
        except OSError:
            gained = b""
        end = gained.rfind(b"\n") + 1
        self.offset += end
        if any(_is_progress(line) for line in gained[:end].splitlines()):
            self.since = now
        return (now - self.since).total_seconds()


def _is_progress(line: bytes) -> bool:
    try:
        event = json.loads(line)
    except ValueError:
        return False
    return isinstance(event, dict) and event.get("type") in ("assistant", "user")


@dataclass
class RunningAttempt:
    handle: AttemptHandle
    request: AttemptRequest
    head_before: str
    started: datetime
    ignored_before: frozenset[str]
    silence: Silence
    terminal: AttemptTerminal | None = None


@dataclass
class RunningProbe:
    process: Process
    dir: ProbeDir
    key: dict[str, Any]
    timeout: float
    started: datetime


@dataclass
class TicketRuntime:
    """A TicketRuntime holds what the Orchestrator keeps in memory about a Ticket between ticks.

    Only `apply` changes the fields that events tell, and `running`, `probe` and `marked` are what the live process
    holds besides: the agent and the Probe at work and the line the view shows.
    `rounds` numbers each Stage's Rounds over the whole Ticket, and `rounds_since_reset` counts them for
    the round limit and model escalation since `reset_rounds` last started the count over.
    `artifacts` lists what the Ticket's Attempts and Probes left, in order, and `cause` is the one whose
    outcome led to the current node.
    `probes` numbers each Gate's Probes over the whole Ticket, and `gate_attempt` counts them since the Ticket
    last entered its current Gate.
    `probe_results` keeps each Gate's latest finished Probe with its number, for the Gates that read it with `from`.
    `spent` counts the Attempts of the current Round that count toward the Stage's `attempts`.
    `sandbox_losses` counts the Attempts and Probes of the whole Ticket that its Sandbox lost.
    `conflict` is the merge `sync` left in the Workspace until the host commits it after `resolve`.
    `synced` is the Integration branch tip the Ticket branch last took in, `base` at claim and then each `sync`'s.
    The Brief and `min_changed` diff from it, so they see only the work of this Ticket.
    `entered_at` is when the Ticket entered its current node, which the duration of a Gate, Operation or Decision
    counts from.
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
    running: RunningAttempt | None = None
    probe: RunningProbe | None = None
    probes: dict[str, int] = field(default_factory=dict)
    probe_results: dict[str, tuple[int, gates.ProbeResult]] = field(default_factory=dict)
    gate_attempt: int = 0
    conflict: Conflict | None = None
    synced: str = ""
    entered_at: datetime | None = None
    waiting: tuple[str, int] | None = None
    waiting_head: str = ""
    answered: bool = False
    marked: str = ""

    @property
    def busy(self) -> bool:
        return self.running is not None or self.probe is not None

    def seconds_in_node(self, now: datetime) -> float | None:
        """Returns how long the Ticket has been in its current node, or None when the journal does not say."""
        return (now - self.entered_at).total_seconds() if self.entered_at else None


# What a TicketRuntime holds that no event tells.
LIVE_ONLY = ("running", "probe", "marked")


@dataclass
class RunMemory:
    """A RunMemory is the state of a Run that its events tell: its Tickets and the parts of the Run outside them.

    `in_flight` and `probing` hold the `AttemptStarted` and `ProbeStarted` payloads of the Tickets whose
    Attempt or Probe has no end event.
    `failures` is the row of Attempts that failed in a row with an infrastructure cause, which opens the Run's Decision.
    `run_waiting` is that Decision while it waits, and `run_asked` counts how often it opened.
    `max_parallel`, `round_limits` and `throttle` are what `limits` last set, and `throttle` starts as the project's.
    `braked` is the window that holds back new Attempts while the Run spends it too fast.
    `landings` lists each merge, and `holder` and `queue` are the Merge window's.
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
    braked: Braked | None = None

    def ticket(self, id: str) -> TicketRuntime | None:
        return next((t for t in self.tickets if t.id == id), None)

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

    def comparable(self) -> dict[str, Any]:
        """Returns the state that events tell, for a check that replaying them gives the live state."""
        tickets = {
            t.id: {f.name: getattr(t, f.name) for f in fields(t) if f.name not in LIVE_ONLY} for t in self.tickets
        }
        return {**{f.name: getattr(self, f.name) for f in fields(self) if f.name != "tickets"}, "tickets": tickets}


def apply(pipeline: Pipeline, memory: RunMemory, type: str, payload: dict[str, Any]) -> None:
    """Applies one event to the Run state: the one place that says what an event does to it."""
    ticket = memory.ticket(payload.get("ticket", ""))
    match type:
        case Event.TicketClaimed if ticket:
            ticket.branch, ticket.base, ticket.synced = payload["branch"], payload["base"], payload["base"]
            ticket.workspace = Path(payload["workspace"])
            ticket.state = TicketState.ACTIVE
        case Event.NodeEntered if ticket:
            ticket.node, ticket.gate_attempt = payload["node"], 0
            if "at" in payload:
                ticket.entered_at = datetime.fromisoformat(payload["at"])
            ticket.cause = payload.get("cause")
            if "round" in payload:
                ticket.round, ticket.attempt, ticket.spent = payload["round"], 0, 0
                ticket.rounds[payload["node"]] = payload["round"]
                ticket.rounds_since_reset[payload["node"]] = payload["rounds_since_reset"]
            if payload["node"] in TERMINAL_NODES and ticket.state in (TicketState.QUEUED, TicketState.ACTIVE):
                ticket.state = TicketState(payload["node"])
        case Event.RoundsReset if ticket:
            for stage in payload["stages"]:
                ticket.rounds_since_reset[stage] = 0
        case Event.AttemptStarted if ticket:
            ticket.attempt = payload["attempt"]
            memory.in_flight[ticket.id] = payload
        case Event.AttemptFinished if ticket:
            memory.in_flight.pop(ticket.id, None)
            _attempt_finished(pipeline, memory, ticket, payload)
        case Event.AttemptLost if ticket:
            memory.in_flight.pop(ticket.id, None)
            _charge(ticket, payload.get("charge"))
        case Event.ProbeStarted if ticket:
            ticket.gate_attempt, ticket.probes[payload["node"]] = payload["attempt"], payload["probe"]
            memory.probing[ticket.id] = payload
        case Event.ProbeInterrupted if ticket:
            memory.probing.pop(ticket.id, None)
        case Event.ProbeLost if ticket:
            memory.probing.pop(ticket.id, None)
            ticket.sandbox_losses += 1
        case Event.GateFinished if ticket:
            memory.probing.pop(ticket.id, None)
            ticket.gate_attempt = payload["attempt"]
            ticket.artifacts.append(payload["artifact"])
            if "from" not in payload:
                ticket.probe_results[payload["node"]] = (payload["probe"], _probe_result(Path(payload["verdict"])))
        case Event.DecisionWaiting if payload["ticket"] == RUN_LEVEL:
            _run_asked(memory, payload)
        case Event.DecisionWaiting if ticket:
            ticket.waiting, ticket.waiting_head = (payload["node"], payload["round"]), payload["head"]
        case Event.DecisionDecided | Event.DecisionOverridden if payload["ticket"] == RUN_LEVEL:
            memory.run_waiting, memory.failures = None, []
        case Event.DecisionDecided | Event.DecisionOverridden if ticket:
            ticket.waiting = None
            ticket.answered |= deciders.KINDS[payload["decider"]].waits
            ticket.artifacts.append(payload["artifact"])
        case Event.OperationFinished if ticket:
            _operation_finished(memory, ticket, payload)
        case Event.ConflictDropped if ticket:
            ticket.conflict = None
        case Event.AbandonedBranchSaved if ticket:
            ticket.kept = True
        case Event.MergeWindowQueued:
            memory.queue.append(payload["ticket"])
        case Event.MergeWindowLeft:
            memory.queue.remove(payload["ticket"])
        case Event.TicketCancelled if ticket:
            ticket.waiting = None
            if ticket.state == TicketState.QUEUED:
                ticket.state = TicketState.ABANDONED
        case Event.LimitsChanged:
            memory.max_parallel = payload["max_parallel"] or memory.max_parallel
            memory.round_limits.update(payload["rounds"])
            for window, shares in payload.get("throttle", {}).items():
                if shares is None:
                    memory.throttle.pop(window, None)
                else:
                    memory.throttle[window] = replace(memory.throttle.get(window, Brake()), **shares)
        case Event.ThrottleOn:
            memory.braked = Braked.of(payload)
        case Event.ThrottleOff:
            memory.braked = None
        case Event.MergeWindowGranted:
            memory.queue.remove(payload["ticket"])
            memory.holder = payload["ticket"]
        case Event.MergeWindowReleased:
            memory.holder = None
        case Event.RunPaused:
            memory.paused_until = datetime.fromisoformat(payload["resets_at"])
        case Event.RunResumed:
            memory.paused_until = None


def _charge(ticket: TicketRuntime, counter: str | None) -> None:
    if counter == SPENT:
        ticket.spent += 1
    elif counter == SANDBOX_LOSSES:
        ticket.sandbox_losses += 1


def _attempt_finished(pipeline: Pipeline, memory: RunMemory, ticket: TicketRuntime, payload: dict[str, Any]) -> None:
    """Applies a finished Attempt: a failed one charges what its `charge` names and joins the row of failures,
    a done one ends the row, leaves its Stage output as an artifact and ends the merge a `commits` Stage resolved.
    """
    if payload["outcome"] == "failed":
        charge = payload.get("charge")
        _charge(ticket, charge)
        if payload["cause"] in INFRA_FAILURES:
            if memory.run_waiting is None:
                failure = InfraFailure(ticket.id, payload["cause"], payload["node"], payload["round"], charge)
                memory.failures = memory.row_with(failure)
        elif payload["cause"] != "rate_limited":
            memory.failures = []
        return
    memory.failures = []
    if "artifact" in payload:
        ticket.artifacts.append(payload["artifact"])
    node = pipeline.nodes[payload["node"]]
    if isinstance(node, Stage) and node.commits and ticket.conflict is not None:
        ticket.synced, ticket.conflict = ticket.conflict.tip, None


def _run_asked(memory: RunMemory, payload: dict[str, Any]) -> None:
    """Applies the opening of the Run's Decision, which gives back what the Attempts of the row charged."""
    memory.run_waiting, memory.run_asked = (payload["node"], payload["round"]), payload["round"]
    for given in payload.get("gave_back", []):
        ticket = memory.ticket(given["ticket"])
        if ticket is not None and given["counter"] == SPENT:
            ticket.spent = max(0, ticket.spent - 1)
        elif ticket is not None:
            ticket.sandbox_losses = max(0, ticket.sandbox_losses - 1)


def _operation_finished(memory: RunMemory, ticket: TicketRuntime, payload: dict[str, Any]) -> None:
    if payload["kind"] == "sync" and payload["outcome"] == "clean":
        ticket.synced = payload["before"]
    elif payload["kind"] == "sync":
        ticket.conflict = Conflict(payload["before"], payload["files"])
        ticket.artifacts.append(payload["artifact"])
    elif payload["outcome"] == "merged":
        ticket.state, ticket.merged_commit = TicketState.MERGED, payload["after"]
        memory.landings.append(Landing(ticket.id, payload["before"], payload["after"]))


def _probe_result(verdict: Path) -> gates.ProbeResult:
    """Rebuilds the finished Probe a verdict judged, whose reports a Gate with `from` reads."""
    root = ProbeDir(verdict.parent)
    written = json.loads(verdict.read_text())
    return gates.ProbeResult(
        gate_out=root.gate_out,
        log=root.log,
        exit_code=written["exit_code"],
        timed_out=written["exit_code"] is None,
        duration=written["duration"],
    )
