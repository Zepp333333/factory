"""The Run view prints the text tree of a Run, its Tickets and their current nodes from the Run's process."""

import os
import select
from pathlib import Path
from typing import TextIO

from factory.commands import submit_decide
from factory.journal import Journal, RunRow, TicketRow, WaitingRow
from factory.states import CommandState, TicketState

ICONS = {
    TicketState.QUEUED: ".",
    TicketState.ACTIVE: "~",
    TicketState.MERGED: "+",
    TicketState.DONE: "+",
    TicketState.ABANDONED: "x",
}


def render_tree(journal: Journal, run_id: str) -> str:
    run = journal.run(run_id)
    assert run is not None
    lines = [f"Run {run.id}  [{run.state}]  {run.integration_branch}{pause_note(run)}"]
    waiting = {w.ticket: w for w in journal.waiting(run_id)}
    for ticket in journal.tickets(run_id):
        lines.append(
            f"  {ICONS[TicketState(ticket.state)]} {ticket.id:<12} {ticket.node or '-':<12} {_where(ticket, waiting)}"
        )
    return "\n".join(lines) + "\n"


def pause_note(run: RunRow) -> str:
    return f"  paused until {run.paused_until}" if run.paused_until else ""


def _where(ticket: TicketRow, waiting: dict[str, WaitingRow]) -> str:
    if ticket.id in waiting:
        return f"waiting for you at {waiting[ticket.id].node}"
    if ticket.last_stage is None:
        return ticket.state
    return f"{ticket.state}, {ticket.last_stage} round {ticket.round}, attempt {ticket.attempt}"


class RunView:
    """RunView prints the tree whenever it changes, redrawing in place on a TTY and appending otherwise.

    When `stdin` is a TTY it also shows the question card of a waiting Decision and takes the owner's answer,
    `<outcome> [reasons]`, as the same Command `factory decide` puts in the inbox.
    With several Decisions waiting it asks the one waiting longest first, and moves on once that one is decided,
    wherever the answer came from.
    """

    def __init__(self, out: TextIO, stdin: TextIO | None = None) -> None:
        self._out = out
        self._stdin = stdin
        self._last = ""
        self._asking: tuple[str, str, int] | None = None
        self._typed = b""
        self._sent: list[tuple[int, str]] = []

    def ring(self) -> None:
        """Signals with BEL that a Decision waits for the owner."""
        self._out.write("\a")
        self._out.flush()

    def prompt(self, journal: Journal, run_id: str) -> None:
        """Reports how the Orchestrator took the answers typed here, then asks the next waiting Decision.

        A line typed while a card was shown belongs to that card, so it is dropped when the card is decided elsewhere.
        """
        for sent in list(self._sent):
            command_id, ticket = sent
            row = journal.command(command_id)
            if row.state != CommandState.PENDING:
                self._sent.remove(sent)
                verdict = (
                    row.result if row.state == CommandState.ACCEPTED else f"{ticket}: answer rejected, {row.result}"
                )
                self._say(f"{verdict}\n")
        if self._stdin is None or not self._stdin.isatty():
            return
        self._read()
        waiting = journal.waiting(run_id)
        current = waiting[0] if waiting else None
        key = (current.ticket, current.node, current.round) if current else None
        if key != self._asking:
            if self._asking is not None and self._typed:
                self._typed = b""
                self._say(
                    f"dropped what was typed for the Decision of {self._asking[0]}, which was decided elsewhere\n"
                )
            self._asking = key
            if current is not None:
                self._say(f"\n{Path(current.card).read_text()}\n> ")
        if current is None:
            return
        line = self._line()
        if not line:
            return
        outcome, _, reason = line.partition(" ")
        reasons = [reason.strip()] if reason.strip() else []
        self._sent.append(
            (submit_decide(journal, run_id, current.ticket, outcome, reasons, "terminal", current), current.ticket)
        )

    def _read(self) -> None:
        """Takes in what was typed on stdin since the last tick, unless a whole line already waits to be sent."""
        assert self._stdin is not None
        try:
            fd = self._stdin.fileno()
            if b"\n" not in self._typed and select.select([fd], [], [], 0)[0]:
                self._typed += os.read(fd, 4096)
        except (OSError, ValueError):
            return

    def _line(self) -> str:
        """Returns the next whole line taken in from stdin, or an empty text when none is complete yet."""
        line, newline, rest = self._typed.partition(b"\n")
        if not newline:
            return ""
        self._typed = rest
        return line.decode(errors="replace").strip()

    def _say(self, text: str) -> None:
        """Writes text between redraws of the tree, so the next redraw appends instead of overwriting it."""
        self._out.write(text)
        self._out.flush()
        self._last = ""

    def show(self, journal: Journal, run_id: str) -> None:
        tree = render_tree(journal, run_id)
        if tree == self._last:
            return
        if self._last and self._out.isatty():
            height = self._last.count("\n")
            self._out.write(f"\x1b[{height}F\x1b[J")
        self._last = tree
        self._out.write(tree)
        self._out.flush()
