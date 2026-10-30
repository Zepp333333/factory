"""The timeline of a Run lists its nodes in the order the Tickets entered them, one line per entry.

A line joins the `NodeEntered` with what the node ended with: the Attempts of a Stage, the Probe of a Gate,
the result of an Operation or the ruling of a Decision.
"""

from dataclasses import dataclass, field
from datetime import datetime

from factory.events import Event
from factory.journal import EventRow

GATE_REASONS_SHOWN = 2


@dataclass(frozen=True)
class _Line:
    at: datetime
    ticket: str
    node: str
    outcome: str
    detail: str


@dataclass
class _Entry:
    at: datetime
    ticket: str
    node: str
    outcome: str = "-"
    details: list[str] = field(default_factory=list)
    started: datetime | None = None
    seconds: float = 0
    turns: int | None = None
    cost: float | None = None


def timeline(events: list[EventRow]) -> list[str]:
    """Returns the lines of the Run: its start, every node entry, the pull request and the end with the cost."""
    lines: list[_Line] = []
    entries: dict[str, _Entry] = {}
    started: datetime | None = None
    total_cost = 0.0

    def close(ticket: str) -> None:
        entry = entries.pop(ticket, None)
        if entry is not None:
            lines.append(_Line(entry.at, entry.ticket, entry.node, entry.outcome, _detail(entry)))

    for event in events:
        payload = event.payload
        ticket = payload.get("ticket", "")
        entry = entries.get(ticket)
        match event.type:
            case Event.RunStarted:
                started = event.at
                lines.append(_Line(event.at, "-", "run", "started", payload["spec_ref"]))
            case Event.NodeEntered:
                close(ticket)
                entries[ticket] = _Entry(event.at, ticket, payload["node"])
            case Event.AttemptStarted if entry:
                entry.started = event.at
            case Event.AttemptFinished if entry:
                if entry.started is not None:
                    entry.seconds += (event.at - entry.started).total_seconds()
                entry.outcome = payload["outcome"]
                entry.details = [payload["cause"]] if "cause" in payload else []
                if "silent" in payload:
                    entry.details.append(f"silent {_duration(payload['silent'])}")
                if usage := payload.get("usage"):
                    entry.turns = (entry.turns or 0) + usage["turns"]
                    entry.cost = (entry.cost or 0) + usage["cost_usd"]
                    total_cost += usage["cost_usd"]
            case Event.GateFinished if entry:
                entry.outcome = payload["outcome"]
                reasons = [reason.strip().splitlines()[0] for reason in payload["reasons"] if reason.strip()]
                entry.details = [f"probe {payload['probe']}", *reasons[:GATE_REASONS_SHOWN]]
            case Event.OperationFinished if entry:
                entry.outcome = payload["outcome"]
                entry.details = [", ".join(payload["files"])] if "files" in payload else []
                if "issues" in payload:
                    entry.details = [", ".join(f"#{issue['number']}" for issue in payload["issues"])]
            case Event.DecisionDecided if entry:
                entry.outcome = payload["outcome"]
                entry.details = [payload["decider"]]
            case Event.PullRequestOpened:
                lines.append(_Line(event.at, "-", "pr", "opened", payload["url"]))
            case Event.RunFinished:
                for open_ticket in list(entries):
                    close(open_ticket)
                took = _duration((event.at - started).total_seconds()) if started else "-"
                lines.append(_Line(event.at, "-", "run", payload["state"], f"{took}  ${total_cost:.2f}"))
    for open_ticket in list(entries):
        close(open_ticket)
    lines.sort(key=lambda line: line.at)
    widths = (
        [max(len(getattr(line, name)) for line in lines) for name in ("ticket", "node", "outcome")] if lines else []
    )
    return [
        f"{line.at:%H:%M:%S}  {line.ticket:<{widths[0]}}  {line.node:<{widths[1]}}  {line.outcome:<{widths[2]}}"
        f"  {line.detail}".rstrip()
        for line in lines
    ]


def _detail(entry: _Entry) -> str:
    parts = [*entry.details]
    if entry.started is not None:
        parts.append(_duration(entry.seconds))
    if entry.turns is not None:
        parts.append(f"{entry.turns} turns")
    if entry.cost is not None:
        parts.append(f"${entry.cost:.2f}")
    return "  ".join(parts)


def _duration(seconds: float) -> str:
    whole = round(seconds)
    return f"{whole} s" if whole < 60 else f"{whole // 60} min {whole % 60} s"
