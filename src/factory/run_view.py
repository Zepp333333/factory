"""The Run view prints the text tree of a Run, its Tickets and their current nodes from the Run's process."""

import base64
import os
import select
import termios
import time
import tty
from pathlib import Path
from typing import TextIO

from factory.commands import submit_decide
from factory.journal import Journal, RunRow, TicketRow, WaitingRow
from factory.states import CommandState, RunState, TicketState

# The terminal answers the probe with `OSC 7501;?` and the states and kinds it reads, ended by ST.
PROBE = "\x1b]7501;?\x1b\\"
PROBE_SECONDS = 0.5
_OSC = "\x1b]7501;"
_ST = "\x1b\\"

TICKET_STATES = {
    TicketState.QUEUED: "idle",
    TicketState.ACTIVE: "working",
    TicketState.MERGED: "done",
    TicketState.DONE: "done",
    TicketState.ABANDONED: "error",
}
RUN_STATES = {
    RunState.RUNNING: "working",
    RunState.DONE: "done",
    RunState.PARTIAL: "error",
    RunState.ABORTED: "error",
}

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


def render_records(journal: Journal, run_id: str) -> str:
    """Renders the tree as OSC 7501 records: the root `app=factory` is the Run, and each child `id=<ticket>` a Ticket.

    A Decision that waits for a human blocks its Ticket and the root with `kind=question`,
    because Orca shows "Needs You" by the state of the root.
    """
    run = journal.run(run_id)
    assert run is not None
    waiting = {w.ticket: w for w in journal.waiting(run_id)}
    root_state = "blocked:kind=question" if waiting else RUN_STATES[RunState(run.state)]
    lines = [_record(root_state, f"{run.state}{pause_note(run)}", app="factory", title=f"Run {run.id}")]
    for ticket in journal.tickets(run_id):
        state = "blocked:kind=question" if ticket.id in waiting else TICKET_STATES[TicketState(ticket.state)]
        lines.append(_record(state, f"{ticket.node or '-'} round {ticket.round}", id=ticket.id))
    return "".join(lines)


def _record(state: str, msg: str, id: str | None = None, app: str | None = None, title: str | None = None) -> str:
    fields = [f"id={id}"] if id else []
    fields.append(f"state={state}")
    if app:
        fields.append(f"app={app}")
    if title:
        fields.append(f"title={_b64(title)}")
    fields.append(f"msg={_b64(msg)}")
    return f"{_OSC}{':'.join(fields)}{_ST}"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


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
        self._probed = False
        self._osc = False
        self._last_records = ""

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
        if not self._probed:
            self._probed = True
            self._osc = self._probe()
            self._report(journal, run_id)
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

    def _probe(self) -> bool:
        """Asks the terminal whether it reads OSC 7501; whatever else it sends back is kept as typed answers."""
        assert self._stdin is not None
        try:
            fd = self._stdin.fileno()
        except (OSError, ValueError):
            return False
        try:
            saved = termios.tcgetattr(fd)
        except termios.error:
            saved = None
        reply = b""
        try:
            if saved is not None:
                tty.setraw(fd)
            self._out.write(PROBE)
            self._out.flush()
            deadline = time.monotonic() + PROBE_SECONDS
            while _ST.encode() not in reply:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([fd], [], [], left)[0]:
                    break
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                reply += chunk
        except OSError:
            return False
        finally:
            if saved is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        head, end, rest = reply.partition(_ST.encode())
        if end and head.startswith((_OSC + "?").encode()):
            self._typed += rest
            return True
        self._typed += reply
        return False

    def _report(self, journal: Journal, run_id: str) -> None:
        """Writes the records of the tree when the terminal reads them and they changed."""
        if not self._osc:
            return
        records = render_records(journal, run_id)
        if records != self._last_records:
            self._last_records = records
            self._out.write(records)
            self._out.flush()

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
            self._report(journal, run_id)
            return
        if self._last and self._out.isatty():
            height = self._last.count("\n")
            self._out.write(f"\x1b[{height}F\x1b[J")
        self._last = tree
        self._out.write(tree)
        self._out.flush()
        self._report(journal, run_id)
