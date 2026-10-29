"""Tracker CLIs: `gh` and `glab` run on the host with the owner's access, and one runner serves both."""

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

from factory import spawn
from factory.tracker import TrackerError

CLI_TIMEOUT_SECONDS = 120
# A projection call waits less, so a Tracker that hangs holds the Run's loop well below a stale heartbeat.
PROJECTION_TIMEOUT_SECONDS = 15
# A failed job's log is kept to its last lines, where the failure is.
LOG_TAIL_LINES = 200


class TrackerCli:
    """TrackerCli runs the owner's CLI named `name` and turns its failures into TrackerError."""

    def __init__(self, name: str, command: Sequence[str], env: Mapping[str, str]) -> None:
        self.name = name
        self.command = tuple(command)
        self.env = dict(env)

    def run(self, *args: str, timeout: float = CLI_TIMEOUT_SECONDS) -> str:
        env = {**os.environ, **self.env}
        try:
            proc = spawn.run([*self.command, *args], env, timeout)
        except subprocess.TimeoutExpired as error:
            raise TrackerError(f"{self.name} {args[0]} timed out") from error
        except OSError as error:
            raise TrackerError(f"cannot run {self.name}: {error}") from error
        if proc.returncode != 0:
            message = proc.stderr.strip().splitlines()
            raise TrackerError(
                f"{self.name} {args[0]} failed: {message[0] if message else f'exit code {proc.returncode}'}"
            )
        return proc.stdout

    def decode(self, text: str) -> list[Any]:
        """Decodes one or more JSON values printed back to back."""
        decoder = json.JSONDecoder()
        values, at = [], 0
        try:
            while (at := _skip_space(text, at)) < len(text):
                value, at = decoder.raw_decode(text, at)
                values.append(value)
        except json.JSONDecodeError as error:
            raise TrackerError(f"{self.name} gave no JSON: {text.strip()[:200]}") from error
        return values


def _skip_space(text: str, at: int) -> int:
    while at < len(text) and text[at].isspace():
        at += 1
    return at


def text_of(body: Any) -> str:
    """Returns an issue body as plain text with `\\n` line ends, empty for no body."""
    return str(body or "").replace("\r\n", "\n").strip()


def tail_of(log: str) -> str:
    """Returns the last lines of a job log."""
    return "\n".join(log.replace("\r\n", "\n").rstrip().splitlines()[-LOG_TAIL_LINES:])
