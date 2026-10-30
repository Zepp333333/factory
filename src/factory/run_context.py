"""The RunContext is the Run an Orchestrator drives: its id, its directory and branch, the Spec and Tracker it works
on, and the state its events tell.

A new Run gets a fresh context, and a Run that resumes or finishes rebuilds it from its events.
Only `memory` changes while the Run goes, and only through the events the Orchestrator journals.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from factory.artifacts import RunDir
from factory.config import Project
from factory.events import Event
from factory.journal import EventRow
from factory.live import LiveTicket
from factory.names import integration_branch
from factory.recovery import replay
from factory.run_state import RunMemory
from factory.tracker import SpecSnapshot, Tracker


@dataclass(frozen=True)
class RunContext:
    run_id: str
    run_dir: RunDir
    integration: str
    snapshot: SpecSnapshot
    tracker: Tracker
    base_commit: str
    memory: RunMemory

    @classmethod
    def new(
        cls, project: Project, home: Path, run_id: str, snapshot: SpecSnapshot, tracker: Tracker, base_commit: str
    ) -> "RunContext":
        """Returns the context of a Run that starts on `snapshot`, with its Integration branch at `base_commit`."""
        memory = RunMemory(
            [LiveTicket(id=t.id, blocked_by=t.blocked_by) for t in snapshot.tickets],
            project.max_parallel,
            throttle=dict(project.throttle),
        )
        return cls(
            run_id, RunDir(home / "runs" / run_id), integration_branch(run_id), snapshot, tracker, base_commit, memory
        )

    @classmethod
    def replayed(
        cls, project: Project, home: Path, run_id: str, tracker: Tracker, events: list[EventRow]
    ) -> "RunContext":
        """Returns the context of a Run that its `events` tell, for a Run that resumes or finishes."""
        snapshot = SpecSnapshot.from_json(
            next(e.payload["snapshot"] for e in events if e.type == Event.SpecSnapshotted)
        )
        base_commit = next(e.payload["commit"] for e in events if e.type == Event.IntegrationBranchCreated)
        tickets = [LiveTicket(id=t.id, blocked_by=t.blocked_by) for t in snapshot.tickets]
        memory = replay(project.pipeline, tickets, events, project.max_parallel, project.throttle)
        return cls(
            run_id, RunDir(home / "runs" / run_id), integration_branch(run_id), snapshot, tracker, base_commit, memory
        )

    @property
    def tickets(self) -> list[LiveTicket]:
        return cast(list[LiveTicket], self.memory.tickets)
