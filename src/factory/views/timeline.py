"""The timeline of a Run lists its nodes in the order the Tickets entered them, one line per entry.

A line joins the `NodeEntered` with what the node ended with: the Attempts of a Stage, the Probe of a Gate,
the result of an Operation or the ruling of a Decision.
`timeline` folds the events through the handlers that their writers registered in `events.times`, so this module
names no event.
"""

from factory.core.events import times
from factory.journal.journal import EventRow
from factory.journal.timeline_memory import TimelineMemory


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
