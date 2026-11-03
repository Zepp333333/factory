"""The Executor port runs a Fresh agent for one Attempt inside the Ticket's Sandbox.

It also defines the Attempt events, the factory's own format of what an agent did,
which the Executor writes to `events.ndjson` next to its raw stream and which every reader outside `adapters/` reads.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from factory.core.states import Failure
from factory.pipeline.pipeline import AgentSettings
from factory.pipeline.throttle import WindowUsage
from factory.ports.sandbox import MemoryReport, Needs, Sandbox


@dataclass(frozen=True)
class AttemptRequest:
    """An AttemptRequest holds what one Attempt runs with.

    `settings` are the node's for the Attempt's Round, after `escalate`.
    `agent_home` is where the agent keeps its own state for the whole Run.
    `kit` is the plugin the Run assembled from the agent kit and the skills of its Library.
    `stream` is the agent's raw output, kept for debugging, and `events` the Attempt events the Executor writes.
    """

    stage: str
    settings: AgentSettings
    prompt: Path
    brief: Path
    output_schema: Path
    attempt_dir: Path
    stream: Path
    events: Path
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


# The raw stream of an Attempt's agent, which only its Executor reads, sits next to its Attempt events in `EVENTS`.
# The Attempt events, one JSON object per line of `events.ndjson`, each with the format's version `v` and its `type`:
# - `progress`: the agent made progress, a new message or a tool result, which a heartbeat never is.
# - `text`: `text` the agent wrote, of `kind` `message`, `thinking` or `note`, a note being what the harness tells.
# - `tool`: a call of the tool `name` on `target`, with `status` `started`,
#   or its end with `status` `done` or `failed` and the tool's `output`.
# - `usage`: what the Attempt spent, with the fields of Usage: `cost_usd`, `turns` and the token totals.
# - `windows`: the `status` of the subscription and when it `resets_at`, and the `windows` by name, each with
#   its `utilization` and `resets_at`.
# - `result`: the agent's last word, with its `status`, `turns`, `duration_ms` and `text`; no event follows it.
# - `error`: `text` the agent wrote that the Executor cannot read, such as a crash message.
# A field the agent does not tell is missing or null, and a reader skips a type it does not know.
STREAM = "stream.ndjson"
EVENTS = "events.ndjson"
EVENTS_VERSION = 1
EVENT_TYPES = ("progress", "text", "tool", "usage", "windows", "result", "error")

type AttemptEvent = dict[str, Any]


def attempt_event(type: str, **fields: Any) -> AttemptEvent:
    """Returns an Attempt event of `type` with `fields`, in the current version of the format."""
    return {"v": EVENTS_VERSION, "type": type, **fields}


def event_line(event: AttemptEvent) -> bytes:
    """Returns the line of `events.ndjson` that holds `event`."""
    return json.dumps(event, ensure_ascii=False).encode() + b"\n"


def read_event(line: bytes) -> AttemptEvent | None:
    """Returns the Attempt event of one line of `events.ndjson`, or None for a line that holds none."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) and event.get("type") in EVENT_TYPES else None


class AttemptHandle(Protocol):
    def poll(self) -> AttemptResult | None: ...

    def kill(self) -> None:
        """Stops the agent and returns once it no longer runs."""
        ...


class Executor(Protocol):
    name: str
    """The name by which the project file and the Pipeline name this Executor."""

    def needs(self) -> Needs:
        """Returns what the agent needs from a Sandbox: the hosts it calls, the env it reads and its credentials."""
        ...

    default_model: str
    """The model an Attempt runs with when neither the Pipeline nor its `defaults` name one."""

    def model_problem(self, name: str) -> str | None:
        """Returns why the Executor cannot run the model `name`, as a hint on what it accepts, or None when it can."""
        ...

    def effort_problem(self, name: str) -> str | None:
        """Returns why the Executor cannot run the reasoning effort `name`, as a hint on what it accepts, or None."""
        ...

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
