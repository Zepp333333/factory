"""Executor port: runs a Fresh agent for one Attempt inside a Sandbox."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from factory.sandbox import Box


@dataclass(frozen=True)
class AttemptRequest:
    stage: str
    model: str
    prompt: Path
    brief: Path
    output_schema: Path
    attempt_dir: Path
    stream: Path


@dataclass(frozen=True)
class AttemptResult:
    """The agent's structured output, or the cause class of its failure."""

    output: dict[str, Any] | None
    failure: str | None


class AttemptHandle(Protocol):
    def poll(self) -> AttemptResult | None: ...


class Executor(Protocol):
    def start(self, request: AttemptRequest, box: Box) -> AttemptHandle: ...
