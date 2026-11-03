"""The TimelineMemory is what the handlers of `events.times` fold the events of a Run into, Line by Line.

The writer of an event registers its handler next to the code that appends it, and `timeline` in `views` renders
the Lines, so the engine and the view share this memory and neither imports the other.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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
