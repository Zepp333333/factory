"""The run entry is where the CLI enters the engine: it builds the Orchestrator of a new Run or of a replayed one.

A new Run resolves its base, gets its id, freezes the Library and starts its RunContext fresh, and a Run that resumes
or finishes rebuilds its RunContext from its events.
Both then get their RunTools, so the CLI knows neither the order of these steps nor the parts of the engine.
"""

import os
import sys
from pathlib import Path

from factory.config import Project
from factory.events import Event, applies, nothing
from factory.git import HostGit
from factory.journal import Journal
from factory.names import integration_branch
from factory.orchestrator import Orchestrator as Orchestrator
from factory.run_context import Ports, RunContext
from factory.run_decision import RunDecisions
from factory.run_start import freeze_library, new_run_id, resolve_base
from factory.run_tools import RunTools
from factory.run_view import RunView
from factory.sleep_block import SleepBlock
from factory.tracker import PullRequestFolder, PullRequests, SpecSnapshot, Tracker


def new_run(
    project: Project,
    tracker: Tracker,
    snapshot: SpecSnapshot,
    ports: Ports,
    pull_requests: PullRequests | PullRequestFolder,
    home: Path,
    journal_path: Path,
    view: RunView,
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
    context = RunContext.new(project, home, run_id, snapshot, tracker, base_commit)
    return _orchestrator(context, journal, project, ports, pull_requests, home, view, slots)


def replayed_run(
    journal: Journal,
    run_id: str,
    project: Project,
    tracker: Tracker,
    ports: Ports,
    pull_requests: PullRequests | PullRequestFolder,
    home: Path,
    view: RunView,
    slots: int,
) -> Orchestrator:
    """Returns the Orchestrator of the Run `run_id` as its events in `journal` tell it, for a Run that resumes or
    finishes."""
    context = RunContext.replayed(project, home, run_id, tracker, journal.events(run_id))
    return _orchestrator(context, journal, project, ports, pull_requests, home, view, slots)


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
    pull_requests: PullRequests | PullRequestFolder,
    home: Path,
    view: RunView,
    slots: int,
) -> Orchestrator:
    """Returns the Orchestrator of the Run of `context`, whose Attempts open the Run's Decision through RunDecisions."""
    tools = RunTools.new(
        context,
        journal,
        project,
        ports,
        home,
        view,
        slots,
        pull_requests,
        lambda cause: RunDecisions(context, tools).ask(cause),
    )
    return Orchestrator(context, tools)


# What the events of the block of sleep tell the readers of the Journal.

applies(Event.SleepBlocked, Event.SleepBlockUnavailable)(nothing)
