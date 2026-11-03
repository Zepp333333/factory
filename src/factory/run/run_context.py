"""The RunContext is the Run an Orchestrator drives: its id, its directory and branch, the Spec and Tracker it works
on, and the state its events tell.

A new Run gets a fresh context, and a Run that resumes or finishes rebuilds it from its events.
Only `memory` changes while the Run goes, and only through the events the Orchestrator journals.
The Ports are the Executor, Sandbox, Workbench and Clock the Run works through, which every phase takes.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from factory.config import Project
from factory.core.clock import Clock
from factory.core.events import Event
from factory.core.names import integration_branch
from factory.journal.journal import EventRow
from factory.journal.recovery import replay
from factory.journal.run_state import RunMemory
from factory.ports.executor import Executor
from factory.ports.sandbox import Sandbox
from factory.ports.tracker import SpecSnapshot, Tracker
from factory.ports.workbench import Workbench
from factory.run.live import LiveTicket
from factory.views.artifacts import RunDir


@dataclass(frozen=True)
class Ports:
    """The Executor, Sandbox, Workbench and Clock one Run works through."""

    executor: Executor
    sandbox: Sandbox
    workbench: Workbench
    clock: Clock


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
