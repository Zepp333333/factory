"""The live state is what the Orchestrator's process holds about a Run that no event tells: the agents, Probes and
terminals at work.

`run_state` keeps only the state the Journal replays, and a LiveTicket adds the live handles to it.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from factory.artifacts import ProbeDir
from factory.executor import AttemptHandle, AttemptRequest, Executor
from factory.run_state import TicketRuntime
from factory.sandbox import Process
from factory.workbench import AttemptTerminal


@dataclass
class Silence:
    """Silence tracks since when an Attempt's agent has made no progress, by what its stream gains.

    The Executor says which line of its stream is progress, so Silence reads no event of any harness.
    """

    stream: Path
    executor: Executor
    since: datetime
    offset: int = 0

    def watch(self, now: datetime) -> float:
        """Reads what the stream gained since the last call, and returns the seconds since the last progress."""
        try:
            with self.stream.open("rb") as file:
                file.seek(self.offset)
                gained = file.read()
        except OSError:
            gained = b""
        end = gained.rfind(b"\n") + 1
        self.offset += end
        if any(self.executor.is_progress(line) for line in gained[:end].splitlines()):
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
    """

    running: RunningAttempt | None = None
    probe: RunningProbe | None = None
    marked: str = ""

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
