"""A RunAgent is a Fresh agent that no Ticket owns, such as the Describer, which writes the PR text, or the Planner,
which selects the Tickets of the Run.

The module that runs an agent declares it and registers it in `RUN_AGENTS`, as `landing` does the Describer,
`ci_fix` the CI fixer and the Addresser, and `nodes/plan` the Planner.
The layers below `run` read every registered agent from there: the Journal frees the slot of one whose work ends, and
`run_view` tells which one works.
A new Run-level agent is a RunAgent its module registers and a kind of `run/nodes/` that names it as its `agent`.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.core.events import Event, applies, nothing


@dataclass(frozen=True)
class RunAgent:
    """A RunAgent is what the factory knows of a Run-level agent before any Pipeline names it.

    `stage` is the stage of its Briefs and Attempts and the Run's phase while it works, and `sandbox` starts the names
    of its Sandbox, branch and Workspace.
    One of `started` begins a piece of its work and gives the Run its phase until one of `ends` comes.
    The agent journals the first of `started`, and the rest are names an older factory journaled.
    An event that more than one agent journals names the agent in its `agent`, and an event without `agent` is one of
    every agent that lists it, as `CiStarted` ends the work of both the CI fixer and the Addresser.
    `doing` words its work from the payload of `started` and the number of its Attempt, 0 when none started yet.
    `requires` lists the fields its Role's schema must require, and only an agent that `reads_body` may have a Role
    with a `body`.
    """

    name: str
    title: str
    stage: str
    sandbox: str
    started: tuple[Event, ...]
    ends: tuple[Event, ...]
    doing: Callable[[dict[str, Any], int], str]
    requires: tuple[str, ...]
    reads_body: bool = False

    @property
    def slot(self) -> str:
        """Returns the name it holds a slot under, which no Ticket id can be, because an id has no "/"."""
        return f"/{self.name}"

    @property
    def start(self) -> Event:
        """Returns the event the agent journals as a piece of its work begins."""
        return self.started[0]

    def names(self, home: Path, run_id: str) -> tuple[str, str, Path]:
        """Returns its Sandbox, branch and Workspace in the Run, none of which a Ticket's can equal.

        Those of a Ticket start with the Run id or sit under its directory, and a Run id starts with a digit.
        """
        name = f"{self.sandbox}-{run_id}"
        return name, f"factory/{name}", home / "workspaces" / name / "workspace"

    def begun_by(self, type: str, payload: dict[str, Any]) -> bool:
        """Tells whether the event `type` with `payload` begins a piece of the agent's work."""
        return type in self.started and payload.get("agent", self.name) == self.name

    def ended_by(self, type: str, payload: dict[str, Any]) -> bool:
        """Tells whether the event `type` with `payload` ends the agent's work."""
        return type in self.ends and payload.get("agent", self.name) == self.name


class RunAgentRegistry:
    """A RunAgentRegistry holds the Run-level agents by name, each registered by the module that runs it.

    It rejects a second agent of one name, since its name keys the agent's slot, Artifacts and events.
    """

    def __init__(self) -> None:
        self._agents: dict[str, RunAgent] = {}

    def register(self, agent: RunAgent) -> RunAgent:
        """Registers `agent` and returns it, so its module declares and registers it in one statement."""
        if agent.name in self._agents:
            raise ValueError(f"a Run-level agent named {agent.name} is registered already")
        self._agents[agent.name] = agent
        return agent

    def unregister(self, name: str) -> None:
        """Removes the agent named `name`."""
        del self._agents[name]

    def __getitem__(self, name: str) -> RunAgent:
        return self._agents[name]

    def __iter__(self) -> Iterator[RunAgent]:
        return iter(tuple(self._agents.values()))


RUN_AGENTS = RunAgentRegistry()


# The clean-up of the Sandbox of any Run-level agent tells the readers of the Journal nothing.
applies(Event.RunAgentSandboxRemoved, Event.RunAgentSandboxLeftBehind, Event.RunAgentCleanupFailed)(nothing)
