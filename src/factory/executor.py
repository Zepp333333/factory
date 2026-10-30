"""The Executor port runs a Fresh agent for one Attempt inside the Ticket's Sandbox."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from factory.pipeline import Profile
from factory.sandbox import MemoryReport, Sandbox
from factory.states import Failure
from factory.throttle import WindowUsage


@dataclass(frozen=True)
class AttemptRequest:
    """An AttemptRequest holds what one Attempt runs with.

    `profile` says whether the agent loads the Target project's own agent config from the Workspace.
    `agent_home` is where the agent keeps its own state for the whole Run.
    `kit` is the plugin the Run assembled from the agent kit and the skills of its Library.
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
    kit: Path


@dataclass(frozen=True)
class Usage:
    """A Usage is what one Attempt spent: its cost in USD, its turns and the tokens of every model it called.

    The cost is the API price equivalent, which is not money charged on a subscription.
    """

    cost_usd: float
    turns: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


@dataclass(frozen=True)
class ModelTokens:
    """ModelTokens are the tokens one model spent in an Attempt, each None when the stream does not tell."""

    input_tokens: int | None
    output_tokens: int | None
    thinking_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None


@dataclass(frozen=True)
class AttemptStats:
    """AttemptStats is what the agent's stream tells about one Attempt, beyond its cost.

    A field the stream does not tell is None and never zero.
    `tokens` is by model and counts the subagents too, while `turns` counts only the main agent's turns,
    so `api_requests` is the count of unique messages of the main agent and its subagents.
    `utilization_start` and `utilization_end` are the share of each subscription window used, by window.
    """

    tokens: dict[str, ModelTokens] | None = None
    api_requests: int | None = None
    turns: int | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None
    subagents_started: int | None = None
    subagents_failed: int | None = None
    subagents_by_type: dict[str, int] | None = None
    tool_calls: dict[str, int] | None = None
    tool_errors: int | None = None
    permission_denials: int | None = None
    peak_context_tokens: int | None = None
    context_window: int | None = None
    utilization_start: dict[str, float] | None = None
    utilization_end: dict[str, float] | None = None


@dataclass(frozen=True)
class AttemptResult:
    """An AttemptResult holds the agent's structured output, or the cause class of its failure and why.

    `usage` is what the Attempt spent, failed or not, and None when it is unknown, as for an agent that gave no result.
    `silent` is how many seconds the agent made no progress before the Orchestrator killed it as `stalled`.
    `memory` is the Sandbox's report on an agent that the OOM killer killed.
    `stats` is what the agent's stream tells about the Attempt, and None for an Attempt that has no stream.
    """

    output: dict[str, Any] | None
    failure: Failure | None
    reason: str | None = None
    resets_at: datetime | None = None
    silent: float | None = None
    memory: MemoryReport | None = None
    usage: Usage | None = None
    stats: AttemptStats | None = None


class AttemptHandle(Protocol):
    def poll(self) -> AttemptResult | None: ...

    def kill(self) -> None:
        """Stops the agent and returns once it no longer runs."""
        ...


class Executor(Protocol):
    name: str
    """The name by which a command line, such as the Attempt renderer's, names this Executor."""

    def reads(self) -> list[Path]:
        """Lists the host paths the agent reads besides the Workspace and the Run directory."""
        ...

    def kit_sha(self, skills: Path) -> str:
        """Returns the hash of the agent config that every agent of this Executor gets, with the Run's `skills`."""
        ...

    def assemble(self, skills: Path, into: Path) -> None:
        """Builds the Run's agent kit plugin in `into`, where a skill in `skills` replaces the kit skill of its name."""
        ...

    def start(self, request: AttemptRequest, sandbox: Sandbox) -> AttemptHandle:
        """Starts the agent, or raises SandboxError when the Sandbox cannot start it."""
        ...

    def windows(self) -> dict[str, WindowUsage]:
        """Returns the latest usage the Executor knows of each subscription window by its name, such as `five_hour`.

        A window the Executor has not heard of yet is missing.
        """
        ...

    def is_progress(self, line: bytes) -> bool:
        """Tells whether one line of the agent's stream shows the agent made progress, for stall detection.

        A heartbeat that keeps coming while a command hangs is no progress.
        """
        ...

    def render_line(self, line: str) -> tuple[str | None, bool]:
        """Returns the text that shows one stream line in the owner's terminal, and whether the line ends the stream.

        None means the line shows nothing.
        """
        ...
