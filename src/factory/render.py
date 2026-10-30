"""The Attempt renderer turns the stream of an agent into lines the owner can follow in any terminal.

It runs as its own process per Attempt, and the Executor named on its command line says what each line shows.
It reads the stream and never writes it, so the raw stream stays an Artifact as the agent wrote it.
"""

import argparse
import os
import select
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from factory.claude_code import ClaudeCodeExecutor
from factory.executor import Executor
from factory.workbench import marker

# The Executors a command line can name.
EXECUTORS: dict[str, Callable[[], Executor]] = {ClaudeCodeExecutor.name: ClaudeCodeExecutor}

POLL_SECONDS = 0.05


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
        self,
        stream: Path,
        executor: Executor,
        out: TextIO | Tee,
        stopped: Callable[[], bool] = lambda: False,
        wake: Wake | None = None,
    ) -> None:
        self._stream = stream
        self._executor = executor
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
            text, is_result = self._executor.render_line(raw.decode(errors="replace"))
            if text:
                print(text, file=self._out, flush=True)
            if is_result:
                return True
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m factory.render")
    parser.add_argument("stream", type=Path, help="the stream.ndjson of an Attempt")
    parser.add_argument(
        "--executor", choices=sorted(EXECUTORS), default=ClaudeCodeExecutor.name, help="the Executor that wrote it"
    )
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
    executor = EXECUTORS[args.executor]()
    if args.log is None:
        Follower(args.stream, executor, sys.stdout).run()
        return 0
    stop = marker(args.log, "stop")
    wake = Wake(sys.stdin.fileno() if args.wake_on_stdin else None)
    with args.log.open("w") as log:
        Follower(args.stream, executor, Tee(log, sys.stdout) if args.echo else log, stop.exists, wake).run()
    marker(args.log, "done").touch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
