"""Metrics are the numbers of a Run, derived from its Journal events by one pure function.

`run_metrics` reads only the events it is given, so `factory stats` and a later exporter call it the same way.
Every number is flat, with its unit in its name, and the fingerprint fields are separate values with few distinct ones.
The `run_id`, the Ticket and the Attempt number are keys of a record and are not part of any name.
A number the events do not tell is None and never zero, so that old Runs show up as unknown.
"""

from dataclasses import dataclass
from datetime import datetime

from factory.core.events import measures
from factory.journal.journal import EventRow
from factory.journal.metrics_memory import AttemptMetrics, MetricsMemory, RunNodeMetrics, TicketMetrics


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
    run_nodes: list[RunNodeMetrics]


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
    memory.leave_run_node(end, None)
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
        cost_usd=memory.cost_usd,
        cost_unknown=sum(t.cost_unknown for t in tickets),
        slots_full_share=(memory.full_for / seconds if seconds else 0.0) if memory.cap is not None else None,
        utilization_spent=spent or None,
        pipeline_sha=run.get("pipeline_sha"),
        kit_sha=run.get("kit_sha"),
        factory_version=run.get("factory_version"),
        attempts=memory.attempts,
        tickets=[t for t in tickets if t.ticket in memory.claimed],
        run_nodes=memory.run_nodes,
    )


def _overlap(start: datetime, stop: datetime, spans: list[tuple[datetime, datetime]]) -> float:
    return sum(max(0.0, (min(stop, b) - max(start, a)).total_seconds()) for a, b in spans)
