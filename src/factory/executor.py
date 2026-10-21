"""The Executor port runs a Fresh agent for one Attempt inside the Ticket's Sandbox."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from factory.sandbox import Sandbox

# Every failed Attempt carries one of these cause classes.
# `agent_config` means the agent loaded more or less than the factory gave it.
# `rate_limited` means the subscription refused the agent until `resets_at`.
Failure = Literal["auth", "sandbox_start", "executor_crash", "timeout", "agent_config", "rate_limited"]
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
    def start(self, request: AttemptRequest, sandbox: Sandbox) -> AttemptHandle:
        """Starts the agent, or raises SandboxError when the Sandbox cannot start it."""
        ...
