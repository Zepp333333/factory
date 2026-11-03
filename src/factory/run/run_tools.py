"""The RunTools are the tools of one Run, which the Orchestrator builds its phases from.

The RunContext holds the Run's frozen values and its state, and the RunTools hold the Journal, the project,
the ports and the parts of the Run that keep state of their own or that more than one phase uses:
the custody of the Run, its merge window, its Attempts, its Decisions and the Run agents.
A phase takes the RunContext and only the tools it uses, so its width shows in its constructor and its imports.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.config import Project
from factory.core.events import Event
from factory.core.git import HostGit
from factory.journal.custody import RunCustody
from factory.journal.journal import Journal
from factory.journal.run_state import apply
from factory.pipeline.pipeline import Pipeline
from factory.ports.run_ports import Ports
from factory.ports.tracker import PullRequests
from factory.run.attempt import Attempts, AttemptTools, Pace
from factory.run.merge_window import MergeWindow
from factory.run.run_agents import RunAgents
from factory.run.run_context import TICK_SECONDS, Append, Display, RunContext
from factory.run.run_decision import RunDecisions


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
    view: Display
    pull_requests: PullRequests
    git: HostGit
    custody: RunCustody
    window: MergeWindow
    attempts: Attempts
    run_decisions: RunDecisions
    run_agents: RunAgents
    append: Append
    beat: Callable[[], None]

    @classmethod
    def new(
        cls,
        context: RunContext,
        journal: Journal,
        project: Project,
        ports: Ports,
        home: Path,
        view: Display,
        slots: int,
        pull_requests: PullRequests,
    ) -> "RunTools":
        """Returns the tools of the Run of `context`, whose Attempts open the Run's Decision through RunDecisions."""

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
        run_decisions = RunDecisions(context, journal, append, view)
        attempts = Attempts(
            context,
            AttemptTools(journal, append, project, ports, git),
            Pace(slots, TICK_SECONDS, beat_and_show),
            run_decisions.ask,
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
            run_decisions,
            RunAgents(context, append, project, ports, home, git, attempts),
            append,
            beat,
        )

    @property
    def pipeline(self) -> Pipeline:
        return self.project.pipeline
