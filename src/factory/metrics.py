"""Metrics are the numbers of a Run, derived from its Journal events by one pure function.

`run_metrics` reads only the events it is given, so `factory stats` and a later exporter call it the same way.
Every number is flat, with its unit in its name, and the fingerprint fields are separate values with few distinct ones.
The `run_id`, the Ticket and the Attempt number are keys of a record and are not part of any name.
A number the events do not tell is None and never zero, so that old Runs show up as unknown.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from factory.events import measures
from factory.fingerprint import KEYS
from factory.journal import EventRow

FINGERPRINT = KEYS


@dataclass(frozen=True)
class AttemptMetrics:
    """AttemptMetrics are the numbers of one finished Attempt and the fingerprint it worked with."""

    ticket: str
    node: str
    round: int
    attempt: int
    outcome: str
    cause: str | None
    prompt_sha: str | None
    schema_sha: str | None
    pipeline_sha: str | None
    kit_sha: str | None
    model: str | None
    effort: str | None
    factory_version: str | None
    cost_usd: float | None
    duration_s: float | None
    turns: int | None
    api_requests: int | None
    input_tokens: int | None
    output_tokens: int | None
    thinking_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    tool_calls: int | None
    tool_errors: int | None
    subagents_started: int | None
    permission_denials: int | None
    peak_context_tokens: int | None

    @property
    def cache_hit_ratio(self) -> float | None:
        """Returns the share of the input that came from the cache, or None when the tokens are unknown."""
        parts = (self.input_tokens, self.cache_read_tokens, self.cache_write_tokens)
        if self.cache_read_tokens is None or any(part is None for part in parts):
            return None
        total = sum(part or 0 for part in parts)
        return (self.cache_read_tokens / total) if total else None


@dataclass(frozen=True)
class GateMetrics:
    """GateMetrics are the numbers of one verdict of a Gate: its outcome on one attempt and what the Probe took."""

    node: str
    attempt: int
    outcome: str
    failed_lines: int | None
    probe_duration_s: float | None


@dataclass(frozen=True)
class ChangeSize:
    node: str
    files: int
    added: int
    removed: int


@dataclass
class TicketMetrics:
    """TicketMetrics are the numbers of one Ticket: how it ended, how it converged and where its time went.

    The `wait_*_s` fields are the time the Ticket spent waiting, which `work_s` leaves out:
    for its blockers to merge, for a free slot, for a rate limit pause to end and for the Merge window.
    """

    ticket: str
    outcome: str = "unfinished"
    by_hand: bool = False
    duration_s: float | None = None
    work_s: float = 0
    cost_usd: float = 0
    cost_unknown: int = 0
    rounds: dict[str, int] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)
    first_pass: bool = True
    fix_rounds: int = 0
    gates: list[GateMetrics] = field(default_factory=list)
    stage_changes: list[ChangeSize] = field(default_factory=list)
    merge_change: ChangeSize | None = None
    workspace_violations: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    wait_blockers_s: float = 0
    wait_slot_s: float = 0
    wait_pause_s: float = 0
    wait_merge_window_s: float = 0
    fingerprints: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class RunMetrics:
    """RunMetrics are the numbers of a Run, with those of its Attempts and Tickets.

    `slots_full_share` is the share of the Run's time in which as many Tickets were at work as its `max_parallel`.
    `utilization_spent` is how much of each subscription window the Run used, from its first Attempt to its last.
    """

    run_id: str
    state: str
    duration_s: float
    cost_usd: float
    cost_unknown: int
    slots_full_share: float | None
    utilization_spent: dict[str, float] | None
    pipeline_sha: str | None
    kit_sha: str | None
    factory_version: str | None
    attempts: list[AttemptMetrics]
    tickets: list[TicketMetrics]


@dataclass
class MetricsMemory:
    """A MetricsMemory is what the handlers of `measures` know of a Run while its events go by.

    `at` is when the event they handle was journaled, and `begin` when the Run started.
    `run` is the payload of `RunStarted`, and `ended` and `state` those of the last `RunFinished`.
    `cap` is the Run's `max_parallel`, `active` the Tickets at work and `full_since` and `full_for` the time in which
    as many Tickets were at work as `cap`.
    `claimed` and `done_at` are when each Ticket was claimed and ended, `blockers` what each waits for, and `gaps`
    the spans in which a Ticket waited for a slot or a pause, which `idle` and `queued` open.
    `fingerprints` are those of the Attempts by Ticket, node, Round and Attempt, and `first_use` and `last_use`
    the use of each subscription window at the first and the last Attempt.
    `pauses` are the spans in which the Run held back new Attempts for a rate limit, and `paused_since` the open one.
    """

    at: datetime
    begin: datetime
    run: dict[str, Any] | None = None
    ended: datetime | None = None
    state: str = "running"
    cap: int | None = None
    tickets: dict[str, TicketMetrics] = field(default_factory=dict)
    attempts: list[AttemptMetrics] = field(default_factory=list)
    fingerprints: dict[tuple[str, str, int, int], dict[str, Any]] = field(default_factory=dict)
    claimed: dict[str, datetime] = field(default_factory=dict)
    gaps: dict[str, list[tuple[datetime, datetime]]] = field(default_factory=dict)
    idle: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    queued: dict[str, datetime] = field(default_factory=dict)
    blockers: dict[str, list[str]] | None = None
    done_at: dict[str, datetime] = field(default_factory=dict)
    active: set[str] = field(default_factory=set)
    full_since: datetime | None = None
    full_for: float = 0.0
    first_use: dict[str, float] = field(default_factory=dict)
    last_use: dict[str, float] = field(default_factory=dict)
    pauses: list[tuple[datetime, datetime]] = field(default_factory=list)
    paused_since: datetime | None = None

    def ticket_of(self, payload: dict[str, Any]) -> TicketMetrics | None:
        """Returns the numbers of the Ticket an event's payload names, which the first event of a Ticket starts."""
        name = payload.get("ticket")
        if name is None:
            return None
        return self.tickets.setdefault(name, TicketMetrics(name))

    def settle(self, at: datetime) -> None:
        """Counts the time from `full_since` to `at` as full when the Tickets at work stopped filling `cap`."""
        full = self.cap is not None and len(self.active) >= self.cap
        if full and self.full_since is None:
            self.full_since = at
        elif not full and self.full_since is not None:
            self.full_for += (at - self.full_since).total_seconds()
            self.full_since = None


def run_metrics(run_id: str, events: list[EventRow]) -> RunMetrics:
    """Derives the metrics of a Run from its events in the order they were journaled.

    Each event goes through the handler its writer registered in `events.measures`, so this module names no event.
    """
    events = [e for e in events if not measures.skips(e.type)]
    memory = MetricsMemory(events[0].at, events[0].at)
    for event in events:
        memory.at = event.at
        memory.ticket_of(event.payload)
        handler = measures.get(event.type)
        if handler is not None:
            handler(memory, event.payload)
    end = memory.ended or events[-1].at
    if memory.paused_since is not None:
        memory.pauses.append((memory.paused_since, end))
    memory.settle(end)
    for name, ticket in memory.tickets.items():
        if ticket.outcome == "unfinished" and name in memory.claimed:
            ticket.duration_s = (end - memory.claimed[name]).total_seconds()
        for start, stop in memory.gaps.get(name, []):
            paused = _overlap(start, stop, memory.pauses)
            ticket.wait_pause_s += paused
            ticket.wait_slot_s += (stop - start).total_seconds() - paused
    seconds = (end - memory.begin).total_seconds()
    spent = {w: max(0.0, memory.last_use[w] - u) for w, u in memory.first_use.items() if w in memory.last_use}
    tickets = memory.tickets.values()
    run = memory.run or {}
    return RunMetrics(
        run_id=run_id,
        state=memory.state,
        duration_s=seconds,
        cost_usd=sum(t.cost_usd for t in tickets),
        cost_unknown=sum(t.cost_unknown for t in tickets),
        slots_full_share=(memory.full_for / seconds if seconds else 0.0) if memory.cap is not None else None,
        utilization_spent=spent or None,
        pipeline_sha=run.get("pipeline_sha"),
        kit_sha=run.get("kit_sha"),
        factory_version=run.get("factory_version"),
        attempts=memory.attempts,
        tickets=[t for t in tickets if t.ticket in memory.claimed],
    )


def blockers_of(snapshot: dict[str, Any]) -> dict[str, list[str]]:
    """Returns the Tickets each Ticket of a Spec snapshot is blocked by."""
    return {t["id"]: list(t.get("blocked_by") or []) for t in snapshot.get("tickets", [])}


def _overlap(start: datetime, stop: datetime, spans: list[tuple[datetime, datetime]]) -> float:
    return sum(max(0.0, (min(stop, b) - max(start, a)).total_seconds()) for a, b in spans)


def size(node: str, payload: dict[str, Any]) -> ChangeSize:
    return ChangeSize(node, payload["files_changed"], payload["lines_added"], payload["lines_removed"])


def spend(ticket: TicketMetrics, payload: dict[str, Any]) -> None:
    usage = payload.get("usage")
    if usage is None:
        ticket.cost_unknown += 1
    else:
        ticket.cost_usd += usage["cost_usd"]


def attempt_metrics(ticket: TicketMetrics, payload: dict[str, Any], fingerprint: dict[str, Any]) -> AttemptMetrics:
    usage = payload.get("usage")
    stats = payload.get("stats") or {}
    models = (stats.get("tokens") or {}).values()

    def tokens(name: str) -> int | None:
        found = [m[name] for m in models if m.get(name) is not None]
        return sum(found) if found else None

    calls = stats.get("tool_calls")
    return AttemptMetrics(
        ticket=ticket.ticket,
        node=payload["node"],
        round=payload["round"],
        attempt=payload["attempt"],
        outcome=payload["outcome"],
        cause=payload.get("cause"),
        cost_usd=usage["cost_usd"] if usage else None,
        duration_s=payload.get("duration"),
        turns=stats.get("turns"),
        api_requests=stats.get("api_requests"),
        input_tokens=tokens("input_tokens"),
        output_tokens=tokens("output_tokens"),
        thinking_tokens=tokens("thinking_tokens"),
        cache_read_tokens=tokens("cache_read_tokens"),
        cache_write_tokens=tokens("cache_write_tokens"),
        tool_calls=sum(calls.values()) if calls is not None else None,
        tool_errors=stats.get("tool_errors"),
        subagents_started=stats.get("subagents_started"),
        permission_denials=stats.get("permission_denials"),
        peak_context_tokens=stats.get("peak_context_tokens"),
        **{name: fingerprint.get(name) for name in FINGERPRINT},
    )
