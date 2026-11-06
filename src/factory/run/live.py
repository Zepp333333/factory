"""The live state is what the Orchestrator's process holds about a Run that no event tells: the agents, Probes and
terminals at work.

`run_state` keeps only the state the Journal replays, and a LiveTicket adds the live handles to it.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from factory.journal.artifacts import ProbeDir
from factory.journal.run_state import TicketRuntime
from factory.ports.executor import AttemptHandle, AttemptRequest, read_event
from factory.ports.sandbox import Process
from factory.ports.workbench import AttemptTerminal


@dataclass
class Silence:
    """Silence tracks since when an Attempt's agent has made no progress, by the `progress` events its Executor writes.

    It reads only the Attempt events, so it reads no event of any harness.
    """

    events: Path
    since: datetime
    offset: int = 0

    def watch(self, now: datetime) -> float:
        """Reads the events written since the last call, and returns the seconds since the last progress."""
        try:
            with self.events.open("rb") as file:
                file.seek(self.offset)
                gained = file.read()
        except OSError:
            gained = b""
        end = gained.rfind(b"\n") + 1
        self.offset += end
        if any((event := read_event(line)) and event["type"] == "progress" for line in gained[:end].splitlines()):
            self.since = now
        return (now - self.since).total_seconds()


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
class LiveTicket(TicketRuntime):
    """A LiveTicket is a TicketRuntime with what the live process holds besides.

    `running` and `probe` are the agent and the Probe at work, and `marked` is the line the view shows.
    `heard` holds, by the input, the new comments read for the next Brief, until that Brief takes them.
    """

    running: RunningAttempt | None = None
    probe: RunningProbe | None = None
    marked: str = ""
    heard: dict[str, list[dict[str, Any]]] | None = None

    @property
    def busy(self) -> bool:
        return self.running is not None or self.probe is not None


@dataclass(frozen=True)
class Ended:
    """Ended is how an Attempt ended, as far as the node it ran for is concerned.

    `outcome` is `done` or `failed` once the node is over, with the `artifact` a done Attempt of a Stage left,
    and None when another Attempt follows.
    `charge` is the counter the Attempt spent, or None when it spent nothing.
    """

    outcome: str | None
    artifact: dict[str, Any] | None = None
    charge: str | None = None


def attempt_key(ticket: LiveTicket) -> dict[str, Any]:
    """The fields that name the Ticket's current Attempt in a Journal row."""
    return {"ticket": ticket.id, "node": ticket.node, "round": ticket.round, "attempt": ticket.attempt}
