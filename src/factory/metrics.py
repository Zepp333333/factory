"""Metrics are the numbers of a Run, derived from its Journal events by one pure function.

`run_metrics` reads only the events it is given, so `factory stats` and a later exporter call it the same way.
Every number is flat, with its unit in its name, and the fingerprint fields are separate values with few distinct ones.
The `run_id`, the Ticket and the Attempt number are keys of a record and are not part of any name.
A number the events do not tell is None and never zero, so that old Runs show up as unknown.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from factory.deciders import OWNER
from factory.events import Event
from factory.journal import EventRow
from factory.pipeline import TERMINAL_NODES

FINGERPRINT = ("prompt_sha", "schema_sha", "pipeline_sha", "kit_sha", "model", "factory_version")


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
    factory_version: str | None
    cost_usd: float | None
    duration_s: float | None
    turns: int | None
    api_requests: int | None
    input_tokens: int | None
    output_tokens: int | None
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
    """GateMetrics are the numbers of one verdict of a Gate: its outcome in a Round and what the Probe took."""

    node: str
    round: int
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


def run_metrics(run_id: str, events: list[EventRow]) -> RunMetrics:
    """Derives the metrics of a Run from its events in the order they were journaled."""
    events = [e for e in events if e.type != Event.Heartbeat]
    started = next((e for e in events if e.type == Event.RunStarted), None)
    begin = started.at if started else events[0].at
    finished = next((e for e in reversed(events) if e.type == Event.RunFinished), None)
    end = finished.at if finished else events[-1].at
    pauses = _pauses(events, end)
    tickets: dict[str, TicketMetrics] = {}
    attempts: list[AttemptMetrics] = []
    fingerprints: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    claimed: dict[str, datetime] = {}
    gaps: dict[str, list[tuple[datetime, datetime]]] = {}
    idle: dict[str, tuple[str, datetime]] = {}
    queued: dict[str, datetime] = {}
    blockers = _blockers(events)
    done_at: dict[str, datetime] = {}
    active: set[str] = set()
    cap: int | None = started.payload.get("max_parallel") if started else None
    full_since: datetime | None = None
    full_for = 0.0
    first_use: dict[str, float] = {}
    last_use: dict[str, float] = {}

    def ticket_of(payload: dict[str, Any]) -> TicketMetrics | None:
        name = payload.get("ticket")
        if name is None:
            return None
        return tickets.setdefault(name, TicketMetrics(name))

    def settle(at: datetime) -> None:
        nonlocal full_since, full_for
        full = cap is not None and len(active) >= cap
        if full and full_since is None:
            full_since = at
        elif not full and full_since is not None:
            full_for += (at - full_since).total_seconds()
            full_since = None

    for event in events:
        payload, at = event.payload, event.at
        ticket = ticket_of(payload)
        match event.type:
            case Event.LimitsChanged:
                cap = payload.get("max_parallel") or cap
                settle(at)
            case Event.TicketClaimed if ticket:
                claimed[ticket.ticket] = at
                active.add(ticket.ticket)
                settle(at)
                ready = max([done_at[b] for b in blockers.get(ticket.ticket, []) if b in done_at], default=begin)
                ready = min(max(ready, begin), at)
                ticket.wait_blockers_s = (ready - begin).total_seconds()
                gaps.setdefault(ticket.ticket, []).append((ready, at))
            case Event.NodeEntered if ticket:
                node = payload["node"]
                if node in TERMINAL_NODES:
                    ticket.outcome = "merged" if node == "done" else "abandoned"
                    done_at[ticket.ticket] = at
                    ticket.duration_s = (at - claimed[ticket.ticket]).total_seconds() if ticket.ticket in claimed else 0
                    active.discard(ticket.ticket)
                    settle(at)
                    idle.pop(ticket.ticket, None)
                    continue
                if "round" in payload:
                    ticket.rounds[node] = max(ticket.rounds.get(node, 0), payload["round"])
                    idle[ticket.ticket] = (node, at)
                    if (payload.get("cause") or {}).get("kind") == "gate":
                        ticket.fix_rounds += 1
                else:
                    idle.pop(ticket.ticket, None)
            case Event.AttemptStarted if ticket:
                node = payload["node"]
                ticket.attempts[node] = ticket.attempts.get(node, 0) + 1
                waited = idle.pop(ticket.ticket, None)
                if waited is not None and waited[0] == node:
                    gaps.setdefault(ticket.ticket, []).append((waited[1], at))
                key = (ticket.ticket, node, payload["round"], payload["attempt"])
                fingerprints[key] = {name: payload.get(name) for name in FINGERPRINT}
                ticket.fingerprints.setdefault(node, fingerprints[key])
            case Event.AttemptFinished if ticket:
                node = payload["node"]
                key = (ticket.ticket, node, payload["round"], payload["attempt"])
                attempt = _attempt(ticket, payload, fingerprints.get(key, {}))
                attempts.append(attempt)
                _spend(ticket, payload)
                ticket.work_s += payload.get("duration") or 0
                if payload["outcome"] == "failed":
                    cause = payload.get("cause") or "unknown"
                    ticket.failures[cause] = ticket.failures.get(cause, 0) + 1
                    idle[ticket.ticket] = (node, at)
                stats = payload.get("stats") or {}
                for window, used in (stats.get("utilization_start") or {}).items():
                    first_use.setdefault(window, used)
                for window, used in (stats.get("utilization_end") or {}).items():
                    last_use[window] = used
            case Event.AttemptLost if ticket:
                ticket.cost_unknown += 1
                ticket.failures[payload["cause"]] = ticket.failures.get(payload["cause"], 0) + 1
                idle[ticket.ticket] = (payload["node"], at)
            case Event.StageBoundaryCommitted if ticket and "files_changed" in payload:
                ticket.stage_changes.append(_size(payload["node"], payload))
            case Event.WorkspaceViolated if ticket:
                ticket.workspace_violations += 1
            case Event.GateFinished if ticket:
                ticket.work_s += payload.get("duration") or 0
                ticket.gates.append(
                    GateMetrics(
                        payload["node"],
                        payload["attempt"],
                        payload["outcome"],
                        payload.get("failed_lines"),
                        payload.get("probe_duration"),
                    )
                )
                if payload["outcome"] == "fail":
                    ticket.first_pass = False
            case Event.OperationFinished if ticket:
                ticket.work_s += payload.get("duration") or 0
                if payload["outcome"] == "merged" and "files_changed" in payload:
                    ticket.merge_change = _size(payload["node"], payload)
            case Event.DecisionDecided if ticket:
                ticket.work_s += payload.get("duration") or 0
                ticket.by_hand = ticket.by_hand or payload.get("decider") == OWNER
            case Event.DecisionOverridden | Event.OwnerEditsCommitted | Event.TicketCancelled if ticket:
                ticket.by_hand = True
            case Event.MergeWindowQueued if ticket:
                queued[ticket.ticket] = at
            case Event.MergeWindowGranted if ticket:
                since = queued.pop(ticket.ticket, None)
                if since is not None:
                    ticket.wait_merge_window_s += (at - since).total_seconds()
    settle(end)
    for name, ticket in tickets.items():
        if ticket.outcome == "unfinished" and name in claimed:
            ticket.duration_s = (end - claimed[name]).total_seconds()
        for start, stop in gaps.get(name, []):
            paused = _overlap(start, stop, pauses)
            ticket.wait_pause_s += paused
            ticket.wait_slot_s += (stop - start).total_seconds() - paused
    seconds = (end - begin).total_seconds()
    spent = {w: max(0.0, last_use[w] - first_use[w]) for w in first_use if w in last_use}
    return RunMetrics(
        run_id=run_id,
        state=str(finished.payload.get("state")) if finished else "running",
        duration_s=seconds,
        cost_usd=sum(t.cost_usd for t in tickets.values()),
        cost_unknown=sum(t.cost_unknown for t in tickets.values()),
        slots_full_share=(full_for / seconds if seconds else 0.0) if cap is not None else None,
        utilization_spent=spent or None,
        pipeline_sha=started.payload.get("pipeline_sha") if started else None,
        kit_sha=started.payload.get("kit_sha") if started else None,
        factory_version=started.payload.get("factory_version") if started else None,
        attempts=attempts,
        tickets=[t for t in tickets.values() if t.ticket in claimed],
    )


def _blockers(events: list[EventRow]) -> dict[str, list[str]]:
    snapshot = next((e.payload["snapshot"] for e in events if e.type == Event.SpecSnapshotted), None)
    return {t["id"]: list(t.get("blocked_by") or []) for t in (snapshot or {}).get("tickets", [])}


def _pauses(events: list[EventRow], end: datetime) -> list[tuple[datetime, datetime]]:
    """Returns the spans in which the Run held back new Attempts for a rate limit."""
    spans: list[tuple[datetime, datetime]] = []
    since: datetime | None = None
    for event in events:
        if event.type == Event.RunPaused and since is None:
            since = event.at
        elif event.type == Event.RunResumed and since is not None:
            spans.append((since, event.at))
            since = None
    if since is not None:
        spans.append((since, end))
    return spans


def _overlap(start: datetime, stop: datetime, spans: list[tuple[datetime, datetime]]) -> float:
    return sum(max(0.0, (min(stop, b) - max(start, a)).total_seconds()) for a, b in spans)


def _size(node: str, payload: dict[str, Any]) -> ChangeSize:
    return ChangeSize(node, payload["files_changed"], payload["lines_added"], payload["lines_removed"])


def _spend(ticket: TicketMetrics, payload: dict[str, Any]) -> None:
    usage = payload.get("usage")
    if usage is None:
        ticket.cost_unknown += 1
    else:
        ticket.cost_usd += usage["cost_usd"]


def _attempt(ticket: TicketMetrics, payload: dict[str, Any], fingerprint: dict[str, Any]) -> AttemptMetrics:
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
        cache_read_tokens=tokens("cache_read_tokens"),
        cache_write_tokens=tokens("cache_write_tokens"),
        tool_calls=sum(calls.values()) if calls is not None else None,
        tool_errors=stats.get("tool_errors"),
        subagents_started=stats.get("subagents_started"),
        permission_denials=stats.get("permission_denials"),
        peak_context_tokens=stats.get("peak_context_tokens"),
        **{name: fingerprint.get(name) for name in FINGERPRINT},
    )
