"""A RunAgent is a Fresh agent that no Ticket owns: the Describer, which writes the PR text, and the CI fixer.

Each is known by its name, and this module holds what every
layer keys by that name: the slot it holds in the Journal, its events, the directory of its Artifacts, the Run's phase
while it works and what its Role's schema must require.
`run_agents` runs one in a Workspace and a Sandbox of its own, and the module of each says what it does with them:
`landing` for the Describer and `ci_fix` for the CI fixer.
Each is the Stage of a Run graph, and a Run Stage with `workspace: integration` is the CI fixer.
A new Run-level agent is a row of `RUN_AGENTS` and a module of its own.
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
    `started` begins a piece of its work and gives the Run its phase until one of `ends` comes, and the clean-up of its
    Sandbox journals `sandbox_removed`, `sandbox_left_behind` or `cleanup_failed`.
    `doing` words its work from the payload of `started` and the number of its Attempt, 0 when none started yet.
    `requires` lists the fields its Role's schema must require, and only an agent that `reads_body` may have a Role
    with a `body`.
    """

    name: str
    title: str
    stage: str
    sandbox: str
    started: Event
    ends: tuple[Event, ...]
    sandbox_removed: Event
    sandbox_left_behind: Event
    cleanup_failed: Event
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


def _fixing(started: dict[str, Any], attempt: int) -> str:
    where = f"Round {started['round']}" + (f", Attempt {attempt}" if attempt else "")
    return f"ci_fix: the fixer works on the red CI of {str(started['commit'])[:7]}, {where}"


DESCRIBER = RunAgent(
    name="describer",
    title="Describer",
    stage="describe",
    sandbox="describer",
    started=Event.DescriberStarted,
    ends=(Event.PullRequestDescribed, Event.DescriberFailed),
    sandbox_removed=Event.DescriberSandboxRemoved,
    sandbox_left_behind=Event.DescriberSandboxLeftBehind,
    cleanup_failed=Event.DescriberCleanupFailed,
    doing=lambda started, attempt: "describe: the Describer writes the PR text",
    requires=("title", "body"),
    reads_body=True,
)

CI_FIXER = RunAgent(
    name="ci_fix",
    title="CI fixer",
    stage="ci_fix",
    sandbox="cifix",
    started=Event.CiFixStarted,
    ends=(Event.CiFixFinished, Event.CiStarted),
    sandbox_removed=Event.CiFixSandboxRemoved,
    sandbox_left_behind=Event.CiFixSandboxLeftBehind,
    cleanup_failed=Event.CiFixCleanupFailed,
    doing=_fixing,
    requires=("summary", "handoff", "failure", "change"),
)

# The Run-level agents by name, in the order the Run's phase is looked for.
RUN_AGENTS = {agent.name: agent for agent in (DESCRIBER, CI_FIXER)}


# What the events of a Run-level agent tell the readers of the Journal: the clean-up of its Sandbox and the start and
# the failure of the Describer tell nothing, and `landing` and `ci_fix` register what the rest tells.

applies(Event.DescriberStarted, Event.DescriberFailed)(nothing)
for agent in RUN_AGENTS.values():
    applies(agent.sandbox_removed, agent.sandbox_left_behind, agent.cleanup_failed)(nothing)
