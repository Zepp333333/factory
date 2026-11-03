"""The RunTools are what every phase of a Run works with, next to the RunContext that says which Run it is.

A phase takes the two and nothing else: the RunContext holds the Run's frozen values and its state,
and the RunTools hold the Journal, the project, the ports and the parts of the Run that keep state of their own,
the custody of the Run, its merge window and its Attempts, which every phase must share.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.attempt import Attempts
from factory.config import Project
from factory.custody import RunCustody
from factory.events import Event
from factory.git import HostGit
from factory.journal import Journal
from factory.merge_window import MergeWindow
from factory.pipeline import Pipeline
from factory.run_context import Ports, RunContext
from factory.run_state import apply
from factory.run_view import RunView
from factory.states import Failure
from factory.tracker import PullRequestFolder, PullRequests

# The Orchestrator ticks, and a phase that waits polls, this often, in seconds.
TICK_SECONDS = 0.2


@dataclass(frozen=True)
class RunTools:
    """RunTools are the tools of one Run that its phases share.

    `append` journals an event and applies it to the Run state, the only way the state changes,
    and `beat` keeps the Run's heartbeat, which raises RunTakenOver when another process took the Run over.
    """

    journal: Journal
    project: Project
    ports: Ports
    home: Path
    view: RunView
    pull_requests: PullRequests | PullRequestFolder
    git: HostGit
    custody: RunCustody
    window: MergeWindow
    attempts: Attempts
    append: Callable[[Event, dict[str, Any]], None]
    beat: Callable[[], None]

    @classmethod
    def new(
        cls,
        context: RunContext,
        journal: Journal,
        project: Project,
        ports: Ports,
        home: Path,
        view: RunView,
        slots: int,
        pull_requests: PullRequests | PullRequestFolder,
        ask_run: Callable[[Failure], None],
    ) -> "RunTools":
        """Returns the tools of the Run of `context`, whose Attempts open the Run's Decision through `ask_run`."""

        def append(type: Event, payload: dict[str, Any]) -> None:
            journal.append(context.run_id, type, payload)
            apply(project.pipeline, context.memory, type, payload)

        custody = RunCustody(journal, ports.clock)

        def beat() -> None:
            custody.beat(context.run_id)

        def beat_and_show() -> None:
            """Beats for the Run and shows the tree, so the phase line of a Run-level agent shows and its time moves."""
            beat()
            view.show(journal, context.run_id)

        git = HostGit(project.repo)
        attempts = Attempts(
            journal, append, project, project.pipeline, ports, git, context, slots, TICK_SECONDS, beat_and_show, ask_run
        )
        return cls(
            journal,
            project,
            ports,
            home,
            view,
            pull_requests,
            git,
            custody,
            MergeWindow(append),
            attempts,
            append,
            beat,
        )

    @property
    def pipeline(self) -> Pipeline:
        return self.project.pipeline
