"""The Attempt renderer turns the Attempt events of an agent into lines the owner can follow in any terminal.

It runs as its own process per Attempt and reads only the events, so it reads alike what any Executor wrote.
It never writes the raw stream, which stays an Artifact as the agent wrote it.
A Workbench that shows a card per Attempt has the renderer hand it the card facts of the events.
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

from factory.ports.executor import AttemptEvent, read_event
from factory.ports.workbench import AttemptCard, marker
from factory.wiring import ATTEMPT_CARDS, events_of_old_attempt

POLL_SECONDS = 0.05

DIM, BOLD, CYAN, YELLOW, RED, RESET = "\x1b[2m", "\x1b[1m", "\x1b[36m", "\x1b[33m", "\x1b[31m", "\x1b[0m"

SHORT_LIMIT = 160
RESULT_LIMIT = 400


def short(value: Any, limit: int = SHORT_LIMIT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_event(event: AttemptEvent) -> str | None:
    """Returns the lines of one Attempt event, or None when it shows nothing, such as progress or usage."""
    kind = event["type"]
    if kind == "text":
        if event.get("kind") == "thinking":
            return f"{DIM}thinking: {short(event.get('text', ''))}{RESET}"
        if event.get("kind") == "note":
            return f"{DIM}{event.get('text')}{RESET}"
        return str(event.get("text", "")) or None
    if kind == "tool":
        if event.get("status") == "started":
            return f"{CYAN}> {event.get('name')}{RESET} {short(event.get('target', ''))}"
        return f"{RED if event.get('status') == 'failed' else DIM}  < {short(event.get('output', ''))}{RESET}"
    if kind == "windows":
        return f"{YELLOW}rate limit {event.get('status')} resets {event.get('resets_at')}{RESET}"
    if kind == "result":
        head = f"{BOLD}result {event.get('status')} turns {event.get('turns')} {event.get('duration_ms')} ms{RESET}"
        text = short(event.get("text") or "", RESULT_LIMIT)
        return f"{head}\n{text}" if text else head
    if kind == "error":
        return str(event.get("text", "")) or None
    return None


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
        self.pipe_closed = False

    def wait(self, seconds: float) -> None:
        if self._fd is None or self.pipe_closed:
            time.sleep(seconds)
            return
        ready, _, _ = select.select([self._fd], [], [], seconds)
        if ready and not os.read(self._fd, 4096):
            self.pipe_closed = True


class NoCard:
    """NoCard is the card of a Workbench that shows none, which takes the card facts and says nothing."""

    def show(self, fact: dict[str, Any]) -> None:
        pass

    def status(self) -> str | None:
        return None

    def close(self) -> None:
        pass


class Follower:
    """A Follower renders the events file as it grows, until the result or until `stopped` says to stop.

    It hands the card facts to `card`, and closes `card` when it is done.
    """

    def __init__(
        self,
        events: Path,
        out: TextIO | Tee,
        stopped: Callable[[], bool] = lambda: False,
        wake: Wake | None = None,
        card: AttemptCard | None = None,
    ) -> None:
        self._events = events
        self._out = out
        self._stopped = stopped
        self._wake = wake or Wake()
        self._card = card or NoCard()
        self._offset = 0
        self._pending = b""

    def run(self) -> None:
        """Renders every line, and returns after the result, or after the lines written before the stop."""
        while True:
            stopping = self._stopped()
            ended = self._step(final=stopping) or stopping
            self._print_card_status()
            if ended:
                self._card.close()
                self._print_card_status()
                return
            self._wake.wait(POLL_SECONDS)

    def _print_card_status(self) -> None:
        status = self._card.status()
        if status:
            print(f"{DIM}{status}{RESET}", file=self._out, flush=True)

    def _step(self, final: bool) -> bool:
        """Renders the new lines and returns True once the result has been shown."""
        if not self._events.is_file():
            return False
        with self._events.open("rb") as events:
            events.seek(self._offset)
            data = events.read()
        self._offset += len(data)
        *lines, self._pending = (self._pending + data).split(b"\n")
        if final and self._pending:
            lines.append(self._pending)
            self._pending = b""
        for line in lines:
            event = read_event(line)
            if event is None:
                continue
            if event["type"] == "card":
                self._card.show(event)
            text = render_event(event)
            if text:
                print(text, file=self._out, flush=True)
            if event["type"] == "result":
                return True
        return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m factory.render")
    parser.add_argument("events", type=Path, help="the events.ndjson of an Attempt")
    parser.add_argument(
        "--log",
        type=Path,
        help="write the lines here, stop once `<log>.stop` exists or stdin closes, and mark the end with `<log>.done`",
    )
    parser.add_argument("--echo", action="store_true", help="with --log, also print the lines, for an owner's terminal")
    parser.add_argument(
        "--stop-on-stdin",
        action="store_true",
        help="with --log, stop once stdin, a pipe, closes, which it does when its parent asks it to or dies",
    )
    parser.add_argument(
        "--card", choices=ATTEMPT_CARDS, help="hand the card facts to the card of this kind, which the Workbench names"
    )
    args = parser.parse_args(argv)
    events_of_old_attempt(args.events)
    card = ATTEMPT_CARDS[args.card]() if args.card else None
    if args.log is None:
        Follower(args.events, sys.stdout, card=card).run()
        return 0
    stop = marker(args.log, "stop")
    wake = Wake(sys.stdin.fileno() if args.stop_on_stdin else None)

    def stopped() -> bool:
        return wake.pipe_closed or stop.exists()

    with args.log.open("w") as log:
        Follower(args.events, Tee(log, sys.stdout) if args.echo else log, stopped, wake, card).run()
    marker(args.log, "done").touch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
