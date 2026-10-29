"""The Attempt renderer turns the `stream-json` of an agent into lines the owner can follow in any terminal.

It prints the assistant's text, tool calls, short results, rate limit events and the final result.
It reads the stream and never writes it, so the raw NDJSON stays an Artifact as the agent wrote it.
"""

import argparse
import json
import os
import select
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

DIM, BOLD, CYAN, YELLOW, RED, RESET = "\x1b[2m", "\x1b[1m", "\x1b[36m", "\x1b[33m", "\x1b[31m", "\x1b[0m"

SHORT_LIMIT = 160
RESULT_LIMIT = 400
POLL_SECONDS = 0.05


def short(value: Any, limit: int = SHORT_LIMIT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_event(event: dict[str, Any]) -> str | None:
    """Returns the lines of one event, or None when it shows nothing, such as a thinking block with no text."""
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        return f"{DIM}model {event.get('model')} tools {len(event.get('tools', []))}{RESET}"
    if kind == "assistant":
        lines = []
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                lines.append(block["text"].rstrip())
            elif block.get("type") == "thinking" and block.get("thinking", "").strip():
                lines.append(f"{DIM}thinking: {short(block['thinking'])}{RESET}")
            elif block.get("type") == "tool_use":
                lines.append(f"{CYAN}> {block.get('name')}{RESET} {short(block.get('input', {}))}")
        return "\n".join(lines) or None
    if kind == "user":
        content = event.get("message", {}).get("content", [])
        results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        lines = [f"{RED if b.get('is_error') else DIM}  < {short(b.get('content', ''))}{RESET}" for b in results]
        return "\n".join(lines) or None
    if kind == "rate_limit_event":
        info = event.get("rate_limit_info", {})
        return f"{YELLOW}rate limit {info.get('status')} resets {info.get('resetsAt')}{RESET}"
    if kind == "result":
        head = (
            f"{BOLD}result {event.get('subtype')} turns {event.get('num_turns')} {event.get('duration_ms')} ms{RESET}"
        )
        text = short(event.get("result") or "", RESULT_LIMIT)
        return f"{head}\n{text}" if text else head
    return None


def render_line(line: str) -> tuple[str | None, bool]:
    """Returns the rendered text of one stream line and whether the line is the final result.

    A line that is not JSON is passed through, so a crash message of the agent still shows.
    """
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return (line.rstrip() or None), False
    if not isinstance(event, dict):
        return line.rstrip(), False
    return render_event(event), event.get("type") == "result"


class Tee:
    """A Tee writes to the log and to the owner's terminal alike."""

    def __init__(self, log: TextIO, terminal: TextIO) -> None:
        self._outs = (log, terminal)

    def write(self, text: str) -> int:
        for out in self._outs:
            out.write(text)
        return len(text)

    def flush(self) -> None:
        for out in self._outs:
            out.flush()


class Wake:
    """A Wake waits between two reads of the stream, and ends the wait early when the pipe on `fd` closes.

    Without a pipe, or once it has closed, it sleeps the whole wait.
    """

    def __init__(self, fd: int | None = None) -> None:
        self._fd = fd

    def wait(self, seconds: float) -> None:
        if self._fd is None:
            time.sleep(seconds)
            return
        ready, _, _ = select.select([self._fd], [], [], seconds)
        if ready and not os.read(self._fd, 4096):
            self._fd = None


class Follower:
    """A Follower renders the stream file as it grows, until the final result or until `stopped` says to stop."""

    def __init__(
        self, stream: Path, out: TextIO | Tee, stopped: Callable[[], bool] = lambda: False, wake: Wake | None = None
    ) -> None:
        self._stream = stream
        self._out = out
        self._stopped = stopped
        self._wake = wake or Wake()
        self._offset = 0
        self._pending = b""

    def run(self) -> None:
        """Renders every line, and returns after the result, or after the lines written before the stop."""
        while True:
            stopping = self._stopped()
            if self._step(final=stopping) or stopping:
                return
            self._wake.wait(POLL_SECONDS)

    def _step(self, final: bool) -> bool:
        """Renders the new lines and returns True once the result has been shown."""
        if not self._stream.is_file():
            return False
        with self._stream.open("rb") as stream:
            stream.seek(self._offset)
            data = stream.read()
        self._offset += len(data)
        *lines, self._pending = (self._pending + data).split(b"\n")
        if final and self._pending:
            lines.append(self._pending)
            self._pending = b""
        for raw in lines:
            text, is_result = render_line(raw.decode(errors="replace"))
            if text:
                print(text, file=self._out, flush=True)
            if is_result:
                return True
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m factory.render")
    parser.add_argument("stream", type=Path, help="the stream.ndjson of an Attempt")
    parser.add_argument(
        "--log",
        type=Path,
        help="write the lines here, stop once `<log>.stop` exists, and mark the end with `<log>.done`",
    )
    parser.add_argument("--echo", action="store_true", help="with --log, also print the lines, for an owner's terminal")
    parser.add_argument(
        "--wake-on-stdin",
        action="store_true",
        help="with --log, look for `<log>.stop` as soon as stdin, a pipe, closes, and not only at the next poll",
    )
    args = parser.parse_args(argv)
    if args.log is None:
        Follower(args.stream, sys.stdout).run()
        return 0
    stop = marker(args.log, "stop")
    wake = Wake(sys.stdin.fileno() if args.wake_on_stdin else None)
    with args.log.open("w") as log:
        Follower(args.stream, Tee(log, sys.stdout) if args.echo else log, stop.exists, wake).run()
    marker(args.log, "done").touch()
    return 0


def marker(log: Path, name: str) -> Path:
    """Returns the file `<log>.stop`, which asks the renderer to finish, or `<log>.done`, which says it has."""
    return log.with_name(f"{log.name}.{name}")


if __name__ == "__main__":
    sys.exit(main())
