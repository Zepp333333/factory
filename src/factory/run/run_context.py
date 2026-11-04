"""The RunContext is the Run an Orchestrator drives: its id, its directory and branch, the Spec it works on and the
Closing of its Tracker, which writes the PR text, and the state its events tell.

A new Run gets a fresh context, and a Run that resumes or finishes rebuilds it from its events.
Only `memory` changes while the Run goes, and only through the events the Orchestrator journals.
The Display is where the Run shows the owner its tree, which `RunView` in `views` implements.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from factory.config import Project
from factory.core.events import Event
from factory.core.names import integration_branch, sandbox_name
from factory.journal.artifacts import RunDir
from factory.journal.journal import EventRow, Journal
from factory.journal.recovery import replay
from factory.journal.run_state import RunMemory
from factory.ports.tracker import Closing, SpecSnapshot
from factory.run.live import LiveTicket

# The Orchestrator ticks, and a phase that waits polls, this often, in seconds.
TICK_SECONDS = 0.2

# Journals an event of the Run and applies it to the Run state, the only way the state changes.
Append = Callable[[Event, dict[str, Any]], None]


class Display(Protocol):
    """The Display is where a Run shows the owner its tree and asks the Decisions that wait, such as `RunView`.

    `views` implements it, so the engine shows a Run without importing a view.
    """

    def ring(self) -> None: ...

    def prompt(self, journal: Journal, run_id: str) -> None: ...

    def show(self, journal: Journal, run_id: str) -> None: ...

    def tick(self, journal: Journal, run_id: str) -> None: ...


@dataclass(frozen=True)
class RunContext:
    run_id: str
    run_dir: RunDir
    integration: str
    snapshot: SpecSnapshot
    closing: Closing
    base_commit: str
    memory: RunMemory

    @classmethod
    def new(
        cls, project: Project, home: Path, run_id: str, snapshot: SpecSnapshot, closing: Closing, base_commit: str
    ) -> "RunContext":
        """Returns the context of a Run that starts on `snapshot`, with its Integration branch at `base_commit`."""
        memory = RunMemory(
            [LiveTicket(id=t.id, blocked_by=t.blocked_by) for t in snapshot.tickets],
            project.max_parallel,
            throttle=dict(project.throttle),
            budgets=dict(project.budgets),
            executor=project.executor.kind,
        )
        return cls(
            run_id, RunDir(home / "runs" / run_id), integration_branch(run_id), snapshot, closing, base_commit, memory
        )

    @classmethod
    def replayed(
        cls, project: Project, home: Path, run_id: str, closing: Closing, events: list[EventRow]
    ) -> "RunContext":
        """Returns the context of a Run that its `events` tell, for a Run that resumes or finishes."""
        snapshot = SpecSnapshot.from_json(
            next(e.payload["snapshot"] for e in events if e.type == Event.SpecSnapshotted)
        )
        base_commit = next(e.payload["commit"] for e in events if e.type == Event.IntegrationBranchCreated)
        tickets = [LiveTicket(id=t.id, blocked_by=t.blocked_by) for t in snapshot.tickets]
        memory = replay(
            project.pipeline,
            tickets,
            events,
            project.max_parallel,
            project.throttle,
            project.budgets,
            executor=project.executor.kind,
        )
        return cls(
            run_id, RunDir(home / "runs" / run_id), integration_branch(run_id), snapshot, closing, base_commit, memory
        )

    @property
    def tickets(self) -> list[LiveTicket]:
        return cast(list[LiveTicket], self.memory.tickets)

    def sandbox(self, ticket: str) -> str:
        """Returns the name of the Sandbox of `ticket` in this Run."""
        return sandbox_name(self.run_id, ticket)
