"""The timeline of a Run lists its nodes in the order the Tickets entered them, one line per entry.

A line joins the `NodeEntered` with what the node ended with: the Attempts of a Stage, the Probe of a Gate,
the result of an Operation or the ruling of a Decision.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from factory.journal import EventRow

REASON_LINES = 2


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
            case "RunStarted":
                started = event.at
                lines.append(_Line(event.at, "-", "run", "started", payload["spec_ref"]))
            case "NodeEntered":
                close(ticket)
                entries[ticket] = _Entry(event.at, ticket, payload["node"])
            case "AttemptStarted" if entry:
                entry.started = event.at
            case "AttemptFinished" if entry:
                if entry.started is not None:
                    entry.seconds += (event.at - entry.started).total_seconds()
                entry.outcome = payload["outcome"]
                entry.details = [payload["cause"]] if "cause" in payload else []
                result = _result(payload["node"], payload["round"], payload["attempt"], event, events)
                if result is not None:
                    entry.turns = (entry.turns or 0) + result.get("num_turns", 0)
                    cost = result.get("total_cost_usd", 0)
                    entry.cost = (entry.cost or 0) + cost
                    total_cost += cost
            case "GateFinished" if entry:
                entry.outcome = payload["outcome"]
                entry.details = [f"probe {payload['probe']}", *payload["reasons"][:REASON_LINES]]
            case "OperationFinished" if entry:
                entry.outcome = payload["outcome"]
                if "artifact" in payload:
                    files = json.loads(Path(payload["artifact"]).read_text())["files"]
                    entry.details = [", ".join(files)]
            case "DecisionDecided" if entry:
                entry.outcome = payload["outcome"]
                entry.details = [payload["decider"]]
            case "PullRequestOpened":
                lines.append(_Line(event.at, "-", "pr", "opened", payload["url"]))
            case "RunFinished":
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


def _result(node: str, round: int, attempt: int, finished: EventRow, events: list[EventRow]) -> dict[str, Any] | None:
    """Returns the `result` line of the Attempt's `stream.ndjson`, or None when the stream has none."""
    for event in events:
        started = event.payload
        if event.type == "AttemptStarted" and (
            started["ticket"],
            started["node"],
            started["round"],
            started["attempt"],
        ) == (finished.payload["ticket"], node, round, attempt):
            stream = Path(started["stream"])
            if not stream.is_file():
                return None
            for line in reversed(stream.read_text(errors="replace").splitlines()):
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and data.get("type") == "result":
                    return data
    return None
