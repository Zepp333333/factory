"""The run entry is where the CLI enters the engine: it builds the Orchestrator of a new Run or of a replayed one.

A new Run resolves its base, gets its id, freezes the Library and starts its RunContext fresh, and a Run that resumes
or finishes rebuilds its RunContext from its events.
Both then get their RunTools, so the CLI knows neither the order of these steps nor the parts of the engine.
"""

import os
import sys
from pathlib import Path

from factory.config import Project
from factory.core.events import Event, applies, nothing
from factory.core.git import HostGit
from factory.core.names import integration_branch
from factory.core.sleep_block import SleepBlock
from factory.journal.journal import Journal
from factory.ports.run_ports import Ports
from factory.ports.tracker import PullRequests, SpecSnapshot, Tracker
from factory.run.orchestrator import Orchestrator as Orchestrator
from factory.run.run_context import Display, RunContext
from factory.run.run_start import freeze_library, new_run_id, resolve_base
from factory.run.run_start import frozen_library as frozen_library
from factory.run.run_tools import RunTools


def new_run(
    project: Project,
    tracker: Tracker,
    snapshot: SpecSnapshot,
    ports: Ports,
    pull_requests: PullRequests,
    home: Path,
    journal_path: Path,
    view: Display,
    slots: int,
) -> Orchestrator:
    """Returns the Orchestrator of a new Run of `snapshot`, which lands through `pull_requests`.

    The Integration branch starts at the base fetched from the remote the Run pushes to,
    and a Run that pushes to no git remote starts from the local base and says so.
    """
    git = HostGit(project.repo)
    base_commit = resolve_base(git, project.base, pull_requests)
    if base_commit is None:
        print(f"factory: the Run pushes to no git remote, so it starts from the local {project.base}", file=sys.stderr)
        base_commit = git.resolve(project.base)
    journal = Journal(journal_path, ports.clock)
    run_id = new_run_id(ports.clock)
    print(f"Run {run_id} started on {integration_branch(run_id)}", flush=True)
    freeze_library(project, home, run_id)
    context = RunContext.new(project, home, run_id, snapshot, tracker.closing, base_commit)
    return _orchestrator(context, journal, project, ports, pull_requests, tracker, home, view, slots)


def replayed_run(
    journal: Journal,
    run_id: str,
    project: Project,
    tracker: Tracker,
    ports: Ports,
    pull_requests: PullRequests,
    home: Path,
    view: Display,
    slots: int,
) -> Orchestrator:
    """Returns the Orchestrator of the Run `run_id` as its events in `journal` tell it, for a Run that resumes or
    finishes."""
    context = RunContext.replayed(project, home, run_id, tracker.closing, journal.events(run_id))
    return _orchestrator(context, journal, project, ports, pull_requests, tracker, home, view, slots)


def keep_awake(orchestrator: Orchestrator, platform: str) -> SleepBlock:
    """Keeps the computer awake while the Run's process lives, and journals the block or why it cannot."""
    block = SleepBlock.acquire(platform, os.getpid())
    if block.reason is None:
        orchestrator.tools.append(Event.SleepBlocked, {"argv": block.argv})
    else:
        orchestrator.tools.append(Event.SleepBlockUnavailable, {"reason": block.reason})
    return block


def _orchestrator(
    context: RunContext,
    journal: Journal,
    project: Project,
    ports: Ports,
    pull_requests: PullRequests,
    tracker: Tracker,
    home: Path,
    view: Display,
    slots: int,
) -> Orchestrator:
    """Returns the Orchestrator of the Run of `context`, which hands the Tracker's board and desk to the RunTools."""
    tools = RunTools.new(
        context, journal, project, ports, home, view, slots, pull_requests, tracker.board, tracker.desk
    )
    return Orchestrator(context, tools)


# What the events of the block of sleep tell the readers of the Journal.

applies(Event.SleepBlocked, Event.SleepBlockUnavailable)(nothing)
