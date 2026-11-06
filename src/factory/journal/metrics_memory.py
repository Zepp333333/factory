"""The MetricsMemory is what the handlers of `events.measures` fold the events of a Run into, Ticket by Ticket.

The writer of an event registers its handler next to the code that appends it, and `run_metrics` in `views` derives
the numbers of the Run from the memory, so the engine and the view share this memory and neither imports the other.
A number the events do not tell is None and never zero, so that old Runs show up as unknown.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from factory.pipeline.fingerprint import KEYS

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
    executor: str | None
    model: str | None
    effort: str | None
    factory_version: str | None
    cost_usd: float | None
    cost_source: str | None
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
class RunNodeMetrics:
    """RunNodeMetrics are the numbers of one node of a Run graph, and `outcome` is None for a node the Run did not end.

    `cost_usd` is what the Attempts that ended while the Run was in the node cost, whichever agent ran them.
    """

    node: str
    outcome: str | None = None
    duration_s: float | None = None
    cost_usd: float = 0


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
    `run_nodes` are the nodes of the Run graph the Run entered, and `in_node` is when it entered the open one and what
    the Run had cost by then.
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
    run_nodes: list[RunNodeMetrics] = field(default_factory=list)
    in_node: tuple[datetime, float] | None = None

    def enter_run_node(self, node: str) -> None:
        self.run_nodes.append(RunNodeMetrics(node))
        self.in_node = (self.at, self.cost_usd)

    def leave_run_node(self, at: datetime, outcome: str | None) -> None:
        """Ends the numbers of the open node of the Run graph at `at`, and does nothing when no node is open."""
        if self.in_node is None:
            return
        node, (entered, cost) = self.run_nodes[-1], self.in_node
        node.outcome, node.duration_s, node.cost_usd = outcome, (at - entered).total_seconds(), self.cost_usd - cost
        self.in_node = None

    @property
    def cost_usd(self) -> float:
        return sum(ticket.cost_usd for ticket in self.tickets.values())

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


def blockers_of(snapshot: dict[str, Any]) -> dict[str, list[str]]:
    """Returns the Tickets each Ticket of a Spec snapshot is blocked by."""
    return {t["id"]: list(t.get("blocked_by") or []) for t in snapshot.get("tickets", [])}


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
        # Only one Executor ran Attempts before Usage told where its cost came from, and it reported the cost.
        cost_source=usage.get("cost_source", "reported") if usage else None,
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
