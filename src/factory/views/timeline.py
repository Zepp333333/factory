"""The timeline of a Run lists its nodes in the order the Tickets entered them, one line per entry.

A line joins the `NodeEntered` with what the node ended with: the Attempts of a Stage, the Probe of a Gate,
the result of an Operation or the ruling of a Decision.
`timeline` folds the events through the handlers that their writers registered in `events.times`, so this module
names no event.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from factory.core.events import times
from factory.journal.journal import EventRow


@dataclass(frozen=True)
class Line:
    at: datetime
    ticket: str
    node: str
    outcome: str
    detail: str


@dataclass
class Entry:
    """An Entry is a node a Ticket or a Run-level agent entered and has not left yet, which becomes a Line."""

    at: datetime
    ticket: str
    node: str
    outcome: str = "-"
    details: list[str] = field(default_factory=list)
    started: datetime | None = None
    seconds: float = 0
    turns: int | None = None
    cost: float | None = None
    attempt: int | None = None


@dataclass
class TimelineMemory:
    """A TimelineMemory is what the handlers of `times` know of a Run while its events go by.

    `at` is when the event they handle was journaled.
    `entries` holds the open Entries by the Ticket or the slot of the Run-level agent that entered them.
    """

    at: datetime
    lines: list[Line] = field(default_factory=list)
    entries: dict[str, Entry] = field(default_factory=dict)
    started: datetime | None = None
    total_cost: float = 0.0

    def entry(self, payload: dict[str, Any]) -> Entry | None:
        """Returns the open Entry of the Ticket an event's payload names."""
        return self.entries.get(payload.get("ticket", ""))

    def enter(self, key: str, ticket: str, node: str) -> Entry:
        """Closes the open Entry under `key` and opens one of `node` in its place."""
        self.close(key)
        self.entries[key] = Entry(self.at, ticket, node)
        return self.entries[key]

    def close(self, key: str) -> None:
        entry = self.entries.pop(key, None)
        if entry is not None:
            self.lines.append(Line(entry.at, entry.ticket, entry.node, entry.outcome, _detail(entry)))

    def close_all(self) -> None:
        for key in list(self.entries):
            self.close(key)

    def line(self, node: str, outcome: str, detail: str) -> None:
        """Adds a line of the Run itself, not of a Ticket."""
        self.lines.append(Line(self.at, "-", node, outcome, detail))


def timeline(events: list[EventRow]) -> list[str]:
    """Returns the lines of the Run: its start, every node entry, the pull request and the end with the cost."""
    if not events:
        return []
    memory = TimelineMemory(events[0].at)
    for event in events:
        handler = times.get(event.type)
        if handler is not None:
            memory.at = event.at
            handler(memory, event.payload)
    memory.close_all()
    lines = sorted(memory.lines, key=lambda line: line.at)
    widths = (
        [max(len(getattr(line, name)) for line in lines) for name in ("ticket", "node", "outcome")] if lines else []
    )
    return [
        f"{line.at:%H:%M:%S}  {line.ticket:<{widths[0]}}  {line.node:<{widths[1]}}  {line.outcome:<{widths[2]}}"
        f"  {line.detail}".rstrip()
        for line in lines
    ]


def _detail(entry: Entry) -> str:
    parts = [*entry.details]
    if entry.started is not None:
        parts.append(duration(entry.seconds))
    if entry.turns is not None:
        parts.append(f"{entry.turns} turns")
    if entry.cost is not None:
        parts.append(f"${entry.cost:.2f}")
    return "  ".join(parts)


def duration(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole} s" if whole < 60 else f"{whole // 60} min {whole % 60} s"
