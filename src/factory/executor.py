"""The Executor port runs a Fresh agent for one Attempt inside the Ticket's Sandbox."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from factory.sandbox import Sandbox

# Every failed Attempt carries one of these cause classes.
Failure = Literal["auth", "sandbox_start", "executor_crash", "timeout"]


@dataclass(frozen=True)
class AttemptRequest:
    stage: str
    model: str
    prompt: Path
    brief: Path
    output_schema: Path
    attempt_dir: Path
    stream: Path
    sandbox: str


@dataclass(frozen=True)
class AttemptResult:
    """An AttemptResult holds the agent's structured output, or the cause class of its failure and why."""

    output: dict[str, Any] | None
    failure: Failure | None
    reason: str | None = None


class AttemptHandle(Protocol):
    def poll(self) -> AttemptResult | None: ...

    def kill(self) -> None:
        """Stops the agent and returns once it no longer runs."""
        ...


class Executor(Protocol):
    def start(self, request: AttemptRequest, sandbox: Sandbox) -> AttemptHandle:
        """Starts the agent, or raises SandboxError when the Sandbox cannot start it."""
        ...
