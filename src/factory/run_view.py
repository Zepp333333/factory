"""The Run view prints the text tree of a Run, its Tickets and their current nodes from the Run's process."""

import base64
import os
import select
import termios
import time
import tty
from datetime import datetime
from pathlib import Path
from typing import TextIO

from factory.clock import Clock
from factory.commands import RUN_LEVEL, decide_for, submit
from factory.events import Event
from factory.journal import EventRow, Journal, RunRow, Spend, TicketRow, WaitingRow
from factory.metrics import snapshot_blockers
from factory.states import CommandState, RunState, TicketState

# The terminal answers the probe with `OSC 7501;?` and the states and kinds it reads, ended by ST.
PROBE = "\x1b]7501;?\x1b\\"
PROBE_SECONDS = 0.5
_OSC = "\x1b]7501;"
_ST = "\x1b\\"
BLOCKED_QUESTION = "blocked:kind=question"

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
    RunState.ORPHANED: "error",
}

ICONS = {
    TicketState.QUEUED: ".",
    TicketState.ACTIVE: "~",
    TicketState.MERGED: "+",
    TicketState.DONE: "+",
    TicketState.ABANDONED: "x",
}


def format_duration(seconds: float) -> str:
    """Returns the duration as `41s`, `12m 04s` or `1h 02m 04s`."""
    whole = max(0, int(seconds))
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def format_spend(spend: Spend) -> str:
    """Returns the cost as `$1.37`, or `$1.37+?` when Attempts that gave no usage are left out of it."""
    return f"${spend.cost_usd:.2f}" + ("+?" if spend.unknown else "")


def render_tree(journal: Journal, run_id: str, now: datetime, timed: bool = True) -> str:
    """Renders the text tree at `now`, with the time of the Run and of each Ticket unless `timed` is false."""
    run = journal.run(run_id)
    assert run is not None
    run_tail = [format_duration(run_seconds(run, now))] if timed else []
    lines = [
        "  ".join(
            [
                f"Run {run.id}  [{run.state}]  {run.integration_branch}{pause_note(run)}",
                *run_tail,
                format_spend(run.spend),
            ]
        )
    ]
    waiting = {w.ticket: w for w in journal.waiting(run_id)}
    if RUN_LEVEL in waiting:
        lines.append(f"  ? {'the Run':<12} {waiting[RUN_LEVEL].node:<12} waiting for you at {waiting[RUN_LEVEL].node}")
    tickets = journal.tickets(run_id)
    width = max((len(_where(t, waiting)) for t in tickets if t.claimed_at), default=0)
    for ticket in tickets:
        head = f"  {ICONS[TicketState(ticket.state)]} {ticket.id:<12} {ticket.node or '-':<12} "
        where = _where(ticket, waiting)
        if ticket.claimed_at is None:
            lines.append(head + where)
            continue
        tail = [format_duration(ticket_seconds(ticket, now))] if timed else []
        lines.append(head + "  ".join([where.ljust(width), *tail, format_spend(ticket.spend)]))
    return "\n".join(lines) + "\n"


def run_seconds(run: RunRow, now: datetime) -> float:
    """Returns how long a finished Run went from its start, or how long a running one has been going."""
    return ((run.finished_at or now) - run.started_at).total_seconds()


def ticket_seconds(ticket: TicketRow, now: datetime) -> float:
    """Returns how long a finished Ticket ran from its claim, or how long a running one has been in its node."""
    assert ticket.claimed_at is not None
    if ticket.finished_at is not None:
        return (ticket.finished_at - ticket.claimed_at).total_seconds()
    return (now - (ticket.entered_at or ticket.claimed_at)).total_seconds()


def run_phase(run: RunRow, events: list[EventRow]) -> str:
    """Returns `describe` while a running Run writes its PR text, `ci` while it waits for the CI of its PR,
    and the Run's state otherwise.
    """
    if run.state == RunState.RUNNING:
        for event in reversed(events):
            if event.type == Event.DescriberStarted:
                return "describe"
            if event.type in (Event.PullRequestDescribed, Event.DescriberFailed):
                break
    if ci_commit(run, events) is not None:
        return "ci"
    return run.state


def ci_commit(run: RunRow, events: list[EventRow]) -> str | None:
    """Returns the commit whose CI a running or orphaned Run waits for, or None when it waits for no CI."""
    found = _ci_state(run, events)
    return found[0] if found else None


def _ci_state(run: RunRow, events: list[EventRow]) -> tuple[str, str] | None:
    """Returns the commit of the CI wait a running or orphaned Run is in, with how the CI ended if it did not pass."""
    if run.state not in (RunState.RUNNING, RunState.ORPHANED):
        return None
    ended = ""
    for event in reversed(events):
        payload = event.payload
        match event.type:
            case Event.CiStarted:
                return str(payload["commit"]), ended
            case Event.CiAbsent | Event.RunFinished:
                return None
            case Event.CiFinished if payload["outcome"] in ("passed", "unchecked"):
                return None
            case Event.CiFinished:
                ended = ended or str(payload["outcome"])
            case Event.DecisionDecided if payload["ticket"] == RUN_LEVEL and payload["node"] == "ci_failed":
                if payload["outcome"] == "accept":
                    return None
    return None


def ci_wait(run: RunRow, events: list[EventRow]) -> str | None:
    """Tells what a Run in phase `ci` waits for, such as `waits for CI on 671096f`."""
    found = _ci_state(run, events)
    if found is None:
        return None
    commit, ended = found
    if ended:
        return f"waits for you: CI {'timed out' if ended == 'timeout' else 'failed'} on {commit[:7]}"
    return f"waits for CI on {commit[:7]}"


def waits(tickets: list[TicketRow], events: list[EventRow]) -> dict[str, str]:
    """Tells for each Ticket that has not started what it waits for, or why it never starts.

    A Ticket waits for its unmerged blockers, or for a slot once they are all merged.
    One behind an abandoned blocker, directly or through others, never starts, and the text names that blocker.
    """
    blockers = snapshot_blockers(events)
    states = {t.id: t.state for t in tickets}

    def abandoned_behind(ticket: str, seen: set[str]) -> list[str]:
        found: list[str] = []
        for blocker in blockers.get(ticket, []):
            if blocker in seen:
                continue
            seen.add(blocker)
            if states.get(blocker) == TicketState.ABANDONED:
                found.append(blocker)
            else:
                found += abandoned_behind(blocker, seen)
        return found

    found: dict[str, str] = {}
    for ticket in tickets:
        if ticket.claimed_at is not None or ticket.state not in (TicketState.QUEUED, TicketState.ABANDONED):
            continue
        if dead := abandoned_behind(ticket.id, set()):
            found[ticket.id] = f"never starts, blocked by abandoned {', '.join(map(_ticket_ref, dead))}"
        elif ticket.state == TicketState.QUEUED:
            unmerged = [b for b in blockers.get(ticket.id, []) if states.get(b) != TicketState.MERGED]
            found[ticket.id] = f"waits for {', '.join(map(_ticket_ref, unmerged))}" if unmerged else "waits for a slot"
    return found


def _ticket_ref(ticket: str) -> str:
    return f"#{ticket}" if ticket.isdigit() else ticket


def pause_note(run: RunRow) -> str:
    note = f"  paused until {run.paused_until}" if run.paused_until else ""
    return f"{note}  {run.braked}" if run.braked else note


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
    root_state = BLOCKED_QUESTION if waiting else RUN_STATES[RunState(run.state)]
    phase = run_phase(run, journal.events(run_id))
    wait = ci_wait(run, journal.events(run_id))
    text = f"{phase}{pause_note(run)}" + (f", {wait}" if wait else "")
    lines = [_record(root_state, text, app="factory", title=f"Run {run.id}")]
    for ticket in journal.tickets(run_id):
        state = BLOCKED_QUESTION if ticket.id in waiting else TICKET_STATES[TicketState(ticket.state)]
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

    def __init__(self, out: TextIO, clock: Clock, stdin: TextIO | None = None) -> None:
        self._out = out
        self._clock = clock
        self._stdin = stdin
        self._last = ""
        self._asking: tuple[str, str, int] | None = None
        self._typed = b""
        self._sent: list[tuple[int, str]] = []
        self._probed = False
        self._osc = False
        self._last_records = ""

    def ring(self) -> None:
        """Signals with BEL that a Decision waits for the owner, unless the terminal reads OSC 7501 and shows it."""
        if self._osc:
            return
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
            (submit(journal, run_id, decide_for(current.ticket, outcome, reasons, "terminal", current)), current.ticket)
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
        """Prints the tree when it changed, and on a TTY also when only its clock moved.

        Without a TTY the time is no change, so the log does not grow every second.
        """
        now = self._clock.now()
        tree = render_tree(journal, run_id, now)
        shown = tree if self._out.isatty() else render_tree(journal, run_id, now, timed=False)
        if shown == self._last:
            self._report(journal, run_id)
            return
        if self._last and self._out.isatty():
            height = self._last.count("\n")
            self._out.write(f"\x1b[{height}F\x1b[J")
        self._last = shown
        self._out.write(tree)
        self._out.flush()
        self._report(journal, run_id)

    def tick(self, journal: Journal, run_id: str) -> None:
        """Redraws the tree of a TTY on a tick without events, so the time moves."""
        if self._out.isatty():
            self.show(journal, run_id)
