"""The Executor port runs a Fresh agent for one Attempt inside the Ticket's Sandbox."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from factory.sandbox import Sandbox

# Every failed Attempt carries one of these cause classes.
# `agent_config` means the agent loaded more or less than the factory gave it.
# `rate_limited` means the subscription refused the agent until `resets_at`.
# `sandbox_lost` means the Sandbox died under the agent.
# `conflict_markers` means the agent left conflict markers in files that `sync` left in conflict.
Failure = Literal[
    "auth",
    "sandbox_start",
    "sandbox_lost",
    "executor_crash",
    "timeout",
    "agent_config",
    "rate_limited",
    "conflict_markers",
]

# An Attempt that fails with one of these causes failed because its Sandbox did, not the agent.
SANDBOX_FAILURES: tuple[Failure, ...] = ("sandbox_start", "sandbox_lost")
Profile = Literal["project", "hermetic"]


@dataclass(frozen=True)
class AttemptRequest:
    """An AttemptRequest holds what one Attempt runs with.

    `profile` says whether the agent loads the Target project's own agent config from the Workspace.
    `agent_home` is where the agent keeps its own state for the whole Run.
    """

    stage: str
    model: str
    profile: Profile
    prompt: Path
    brief: Path
    output_schema: Path
    attempt_dir: Path
    stream: Path
    sandbox: str
    workspace: Path
    agent_home: Path


@dataclass(frozen=True)
class AttemptResult:
    """An AttemptResult holds the agent's structured output, or the cause class of its failure and why."""

    output: dict[str, Any] | None
    failure: Failure | None
    reason: str | None = None
    resets_at: datetime | None = None


class AttemptHandle(Protocol):
    def poll(self) -> AttemptResult | None: ...

    def kill(self) -> None:
        """Stops the agent and returns once it no longer runs."""
        ...


class Executor(Protocol):
    def reads(self) -> list[Path]:
        """Lists the host paths the agent reads besides the Workspace and the Run directory."""
        ...

    def start(self, request: AttemptRequest, sandbox: Sandbox) -> AttemptHandle:
        """Starts the agent, or raises SandboxError when the Sandbox cannot start it."""
        ...
