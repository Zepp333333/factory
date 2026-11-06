"""Tracker CLIs: `gh` and `glab` run on the host with the owner's access, and one runner serves both."""

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

from factory.core import spawn
from factory.ports.tracker import Comment, TrackerError

CLI_TIMEOUT_SECONDS = 120
# A projection call, or an action of a Tracker Operation, waits less, so a Tracker that hangs holds the Run's
# loop well below a stale heartbeat.
PROJECTION_TIMEOUT_SECONDS = 15
# A failed job's log is kept to its last lines, where the failure is.
LOG_TAIL_LINES = 200
# The end of each comment the factory posts, which no reader sees rendered: the factory works with the owner's access,
# so only this tells its comments from the owner's.
SIGNATURE = "<!-- software-factory -->"


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


def signed(text: str) -> str:
    """Returns the text of a comment the factory posts, which `comment_of` tells apart when it reads it back."""
    return f"{text}\n\n{SIGNATURE}"


def unsigned(text: str) -> str:
    """Returns the text of a comment without the factory's signature."""
    return text.removesuffix(SIGNATURE).rstrip()


def comment_of(id: str, author: Any, body: Any, **place: Any) -> Comment:
    """Returns the Comment a Tracker read, whose signature says whether the factory posted it."""
    text = text_of(body)
    return Comment(id, str(author), unsigned(text), by_factory=text.endswith(SIGNATURE), **place)


def tail_of(log: str) -> str:
    """Returns the last lines of a job log."""
    return "\n".join(log.replace("\r\n", "\n").rstrip().splitlines()[-LOG_TAIL_LINES:])


def issue_numbers(tickets: Sequence[str] | None) -> list[int] | None:
    """Returns the Ticket ids a Run takes as issue numbers, each once, or raises TrackerError at an id that is none."""
    if tickets is None:
        return None
    for ticket in tickets:
        if not (ticket.isascii() and ticket.isdigit()):
            raise TrackerError(f"--tickets takes issue numbers such as 28,30, not {ticket!r}")
    return list(dict.fromkeys(int(ticket) for ticket in tickets))
