"""A RunAgent is a Fresh agent that no Ticket owns: the Describer, which writes the PR text, the CI fixer and the
Addresser, which fix the PR, and the Planner, which selects the Tickets of the Run.

Each is known by its name, and this module holds what every
layer keys by that name: the slot it holds in the Journal, the events that start and end its work, the directory of
its Artifacts, the Run's phase while it works and what its Role's schema must require.
`run_agents` runs one in a Workspace and a Sandbox of its own, and the module of each says what it does with them:
`landing` for the Describer, `nodes/plan` for the Planner and `ci_fix` for the CI fixer and the Addresser.
Each is the agent of a kind of Run Stage, which names it as its `agent`: `describe`, `plan`, `ci_fix` and `address`.
A new Run-level agent is a row of `RUN_AGENTS` and the kind of a module of `run/nodes/`, as the Planner's is `plan`.
"""

from collections.abc import Callable
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
    An event that more than one agent journals names the agent in its `agent`, and an event without it is the agent's
    own.
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


def _round(started: dict[str, Any], attempt: int) -> str:
    return f"Round {started['round']}" + (f", Attempt {attempt}" if attempt else "")


DESCRIBER = RunAgent(
    name="describer",
    title="Describer",
    stage="describe",
    sandbox="describer",
    started=(Event.DescriberStarted,),
    ends=(Event.PullRequestDescribed, Event.DescriberFailed),
    doing=lambda started, attempt: "describe: the Describer writes the PR text",
    requires=("title", "body"),
    reads_body=True,
)

# An older factory journaled the Rounds of the Addresser too under the events, the slot and the Sandbox of the CI
# fixer, so such a Round reads as the CI fixer's.
CI_FIXER = RunAgent(
    name="ci_fix",
    title="CI fixer",
    stage="ci_fix",
    sandbox="cifix",
    started=(Event.FixStarted, Event.CiFixStarted),
    ends=(Event.FixFinished, Event.CiFixFinished, Event.CiStarted),
    doing=lambda started, attempt: (
        f"ci_fix: the fixer works on the red CI of {str(started['commit'])[:7]}, {_round(started, attempt)}"
    ),
    requires=("summary", "handoff", "failure", "change"),
)

ADDRESSER = RunAgent(
    name="address",
    title="Addresser",
    stage="address",
    sandbox="address",
    started=(Event.FixStarted,),
    ends=(Event.FixFinished, Event.CiStarted),
    doing=lambda started, attempt: (
        f"{started['node']}: the agent works on the comments on the PR, {_round(started, attempt)}"
    ),
    requires=("summary",),
)

PLANNER = RunAgent(
    name="planner",
    title="Planner",
    stage="select",
    sandbox="planner",
    started=(Event.PlannerStarted,),
    ends=(Event.PlannerFinished, Event.PlannerFailed),
    doing=lambda started, attempt: f"select: the Planner selects the Tickets, Attempt {started['attempt']}",
    requires=("tickets", "left_out"),
)

# The Run-level agents by name.
RUN_AGENTS = {agent.name: agent for agent in (DESCRIBER, CI_FIXER, ADDRESSER, PLANNER)}

# The events that end the work of a Run-level agent.
ENDS = tuple(dict.fromkeys(event for agent in RUN_AGENTS.values() for event in agent.ends))


def fixed_by(payload: dict[str, Any]) -> RunAgent:
    """Returns the RunAgent that journaled an event of a Round of a Stage that commits, from its payload."""
    return RUN_AGENTS[payload.get("agent", CI_FIXER.name)]


# What the events of a Run-level agent tell the readers of the Journal: the clean-up of its Sandbox and the start and
# the failure of the Describer and the start of the Planner tell nothing, and `landing` and `ci_fix` register what the
# rest tells.

applies(Event.DescriberStarted, Event.DescriberFailed, Event.PlannerStarted)(nothing)
applies(
    Event.RunAgentSandboxRemoved,
    Event.RunAgentSandboxLeftBehind,
    Event.RunAgentCleanupFailed,
    Event.CiFixSandboxRemoved,
    Event.CiFixSandboxLeftBehind,
    Event.CiFixCleanupFailed,
    Event.DescriberSandboxRemoved,
    Event.DescriberSandboxLeftBehind,
    Event.DescriberCleanupFailed,
    Event.PlannerSandboxRemoved,
    Event.PlannerSandboxLeftBehind,
    Event.PlannerCleanupFailed,
)(nothing)
