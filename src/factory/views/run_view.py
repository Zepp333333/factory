"""The Run view prints the text tree of a Run, its Tickets and their current nodes from the Run's process."""

import os
import re
import select
import shutil
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

from factory.core.clock import Clock
from factory.core.events import Event
from factory.core.run_agent import RUN_AGENTS, RunAgent
from factory.core.states import CommandState, RunState, TicketState
from factory.journal.commands import RUN_LEVEL, decide_for, submit
from factory.journal.journal import EventRow, Journal, RunRow, Spend, TicketRow, WaitingRow
from factory.journal.metrics_memory import blockers_of
from factory.pipeline.throttle import WHOLE_RUN, Braked, split_budgets
from factory.ports.workbench import OwnerInput, RunTree, TicketTree, Workbench

# How the Tracker of a Run shows the id of a Ticket, such as `#12` for an issue number.
Reference = Callable[[str], str]


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


def render_tree(
    journal: Journal,
    run_id: str,
    now: datetime,
    reference: Reference,
    timed: bool = True,
    phase_timed: bool | None = None,
) -> str:
    """Renders the text tree at `now`, with the time of the Run and of each Ticket unless `timed` is false.

    Each Ticket shows as the Tracker's `reference` shows it.
    The phase line carries its time unless `phase_timed`, which is `timed` when not given, is false.
    """
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
    tickets = journal.tickets(run_id)
    if not any(t.state == TicketState.ACTIVE for t in tickets) and (
        note := phase_note(run, journal.events(run_id), now, timed if phase_timed is None else phase_timed)
    ):
        lines.append(f"  {note}")
    waiting = {w.ticket: w for w in journal.waiting(run_id)}
    if RUN_LEVEL in waiting:
        lines.append(f"? the Run: waiting for you at {waiting[RUN_LEVEL].node}")
    header = ["Ticket", "Node", "State", "Stage", "Round", "Attempt", *(["Duration"] if timed else []), "Cost"]
    rows = []
    for ticket in tickets:
        claimed = ticket.claimed_at is not None
        tail = [format_duration(ticket_seconds(ticket, now))] if timed and claimed else [""] if timed else []
        rows.append(
            [
                reference(ticket.id),
                ticket.node or "-",
                _state(ticket, waiting),
                ticket.last_stage or "-",
                str(ticket.round) if ticket.last_stage else "",
                str(ticket.attempt) if ticket.last_stage else "",
                *tail,
                format_spend(ticket.spend) if claimed else "",
            ]
        )
    counts = Counter(t.state for t in tickets)
    total = ["TOTAL", run.state, ", ".join(f"{n} {state}" for state, n in counts.items()), "", "", "", *run_tail]
    total.append(format_spend(run.spend))
    widths = [max(len(row[i]) for row in [header, *rows, total]) for i in range(len(header))]
    rule = ["-" * width for width in widths]
    for row in (header, rule, *rows, rule, total):
        lines.append("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
    return "\n".join(lines) + "\n"


def _state(ticket: TicketRow, waiting: dict[str, WaitingRow]) -> str:
    return f"waiting for you at {waiting[ticket.id].node}" if ticket.id in waiting else ticket.state


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
    """Returns the `stage` of the RunAgent that works for a running Run, `describe` while the Describer writes its PR
    text and `ci_fix` while the CI fixer works on its red CI, `ci` while it waits for the CI of its PR, and the Run's
    state otherwise.
    """
    if (working := _at_work(run, events)) is not None:
        return working[0].stage
    if ci_commit(run, events) is not None:
        return "ci"
    return run.state


def _at_work(run: RunRow, events: list[EventRow]) -> tuple[RunAgent, EventRow, int] | None:
    """Returns the RunAgent that works for a running Run now, the event that started its work and its Attempt, or None.

    The Attempt is 0 when the agent journals no `AttemptStarted` of its own, as the Describer does not.
    """
    if run.state != RunState.RUNNING:
        return None
    for agent in RUN_AGENTS.values():
        attempt = 0
        for event in reversed(events):
            if event.type == Event.AttemptStarted and event.payload["ticket"] == agent.slot and not attempt:
                attempt = event.payload["attempt"]
            elif event.type == agent.started:
                return agent, event, attempt
            elif event.type in agent.ends:
                break
    return None


def ci_commit(run: RunRow, events: list[EventRow]) -> str | None:
    """Returns the commit whose CI a running or orphaned Run waits for, or None when it waits for no CI."""
    found = _ci_state(run, events)
    return found.commit if found else None


@dataclass(frozen=True)
class _CiState:
    """The CI wait a Run is in: the commit and PR, when its `CiStarted` came, and how and when the CI ended if red."""

    commit: str
    url: str
    started: datetime
    ended: str = ""
    ended_at: datetime | None = None


def _ci_state(run: RunRow, events: list[EventRow]) -> _CiState | None:
    """Returns the CI wait a running or orphaned Run is in."""
    if run.state not in (RunState.RUNNING, RunState.ORPHANED):
        return None
    ended, ended_at = "", None
    for event in reversed(events):
        payload = event.payload
        match event.type:
            case Event.CiStarted:
                url = str(payload.get("url", ""))
                return _CiState(str(payload["commit"]), url, event.at, ended, ended_at)
            case Event.CiAbsent | Event.RunFinished:
                return None
            case Event.CiFinished if payload["outcome"] in ("passed", "unchecked"):
                return None
            case Event.CiFinished:
                if not ended:
                    ended, ended_at = str(payload["outcome"]), event.at
            case Event.DecisionDecided if payload["ticket"] == RUN_LEVEL and payload["node"] == "ci_failed":
                if payload["outcome"] == "accept":
                    return None
    return None


def phase_note(run: RunRow, events: list[EventRow], now: datetime, timed: bool = True) -> str | None:
    """Tells what a Run in the phase of a RunAgent or in `ci` does, and for how long unless `timed` is false.

    A Run in another phase gets None.
    """

    def since(start: datetime, end: datetime = now) -> list[str]:
        return [format_duration((end - start).total_seconds())] if timed else []

    if (working := _at_work(run, events)) is not None:
        agent, started, attempt = working
        return ", ".join([agent.doing(started.payload, attempt), *since(started.at)])
    found = _ci_state(run, events)
    if found is None:
        return None
    commit = found.commit[:7]
    if found.ended:
        how = "timed out" if found.ended == "timeout" else "failed"
        after = "".join(f" after {took}" for took in since(found.started, found.ended_at or now))
        return (
            f"ci: waits for you: CI {how} on {commit}{after}, "
            f"answer with `factory decide {run.id} {RUN_LEVEL} recheck` or `accept`"
        )
    return ", ".join([f"ci: waits for CI on {commit}", _pr_ref(found.url), *since(found.started)])


def _pr_ref(url: str) -> str:
    """Returns `PR #181` for a PR URL that ends in its number, and `PR <url>` for any other reference."""
    number = re.search(r"/(\d+)/?$", url)
    return f"PR #{number.group(1)}" if number else f"PR {url}"


def waits(tickets: list[TicketRow], events: list[EventRow], reference: Reference) -> dict[str, str]:
    """Tells for each Ticket that has not started what it waits for, or why it never starts.

    The text shows each blocker as the Tracker's `reference` shows it.

    A Ticket waits for its unmerged blockers, or for a slot once they are all merged.
    One behind an abandoned blocker, directly or through others, never starts, and the text names that blocker.
    """
    snapshot = next((e.payload["snapshot"] for e in events if e.type == Event.SpecSnapshotted), None)
    blockers = blockers_of(snapshot or {})
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
            found[ticket.id] = f"never starts, blocked by abandoned {', '.join(map(reference, dead))}"
        elif ticket.state == TicketState.QUEUED:
            unmerged = [b for b in blockers.get(ticket.id, []) if states.get(b) != TicketState.MERGED]
            found[ticket.id] = f"waits for {', '.join(map(reference, unmerged))}" if unmerged else "waits for a slot"
    return found


def pause_note(run: RunRow) -> str:
    note = f"  paused until {run.paused_until}" if run.paused_until else ""
    if run.braked is None:
        return note
    braked = Braked.of(run.braked)
    return f"{note}  braked by {braked.text} until {braked.until.isoformat()}"


def budget_note(run: RunRow, events: list[EventRow]) -> str | None:
    """Tells what the Run spent of each budget it has, from the project's budgets and what `limits` set since.

    The Run's own cost is its Spend, and an Executor's is the cost of the Attempts that ended on it.
    A Run without a budget gets None.
    """
    budgets: dict[str, float] = {}
    spent: dict[str, float] = {}
    executor_of: dict[str, str] = {}
    for event in events:
        payload = event.payload
        match event.type:
            case Event.RunStarted:
                budgets.update(split_budgets(payload.get("settings", {}).get("throttle") or {})[0])
            case Event.LimitsChanged:
                budgets.update(payload.get("budgets", {}))
            case Event.AttemptStarted:
                executor_of[payload["ticket"]] = payload.get("executor", "")
            case Event.AttemptFinished if payload.get("usage"):
                executor = executor_of.get(payload["ticket"], "")
                spent[executor] = spent.get(executor, 0) + payload["usage"]["cost_usd"]
    if not budgets:
        return None
    parts = [f"{format_spend(run.spend)} of ${budgets[WHOLE_RUN]:.2f}"] if WHOLE_RUN in budgets else []
    parts += [
        f"{name} ${spent.get(name, 0):.2f} of ${budget:.2f}" for name, budget in budgets.items() if name != WHOLE_RUN
    ]
    return f"Budget: {', '.join(parts)}"


def run_tree(journal: Journal, run_id: str, now: datetime) -> RunTree:
    """Builds the value of the Run at `now` that a Workbench shows."""
    run = journal.run(run_id)
    assert run is not None
    events = journal.events(run_id)
    return RunTree(
        run.id,
        run.state,
        f"{phase_note(run, events, now) or run_phase(run, events)}{pause_note(run)}",
        tuple(TicketTree(t.id, t.state, t.node, t.round) for t in journal.tickets(run_id)),
        tuple(w.ticket for w in journal.waiting(run_id)),
    )


def _screen_rows(text: str, columns: int) -> int:
    """Counts the rows text takes on a terminal `columns` wide, where a row rewrapped after a resize takes several."""
    return sum(max(1, -(-len(line) // columns)) for line in text.split("\n")[:-1])


class RunView:
    """RunView prints the tree whenever it changes, redrawing in place on a TTY and appending otherwise.

    When `stdin` is a TTY it also shows the question card of a waiting Decision and takes the owner's answer,
    `<outcome> [reasons]`, as the same Command `factory decide` puts in the inbox.
    With several Decisions waiting it asks the one waiting longest first, and moves on once that one is decided,
    wherever the answer came from.
    Each time it also hands the Workbench the Run tree, and tells it when the owner is needed and no longer is.
    """

    def __init__(
        self, out: TextIO, clock: Clock, reference: Reference, owner_input: OwnerInput, workbench: Workbench
    ) -> None:
        self._out = out
        self._clock = clock
        self._reference = reference
        self._input = owner_input
        self._workbench = workbench
        self._last = ""
        self._drawn = ""
        self._asking: tuple[str, str, int] | None = None
        self._sent: list[tuple[int, str]] = []
        self._owner_waited = False

    def ring(self) -> None:
        """Has the Workbench tell the owner that a Decision waits for them."""
        self._workbench.needs_owner(True)

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
        if self._input.stdin is None or not self._input.stdin.isatty():
            return
        self._read()
        waiting = journal.waiting(run_id)
        current = waiting[0] if waiting else None
        key = (current.ticket, current.node, current.round) if current else None
        if key != self._asking:
            if self._asking is not None and self._input.typed:
                self._input.typed = b""
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

    def _report(self, journal: Journal, run_id: str) -> None:
        """Hands the Workbench the tree, and tells it when no Decision waits for the owner any more."""
        tree = run_tree(journal, run_id, self._clock.now())
        self._workbench.show_run(tree)
        if self._owner_waited and not tree.waiting:
            self._workbench.needs_owner(False)
        self._owner_waited = bool(tree.waiting)

    def _read(self) -> None:
        """Takes in what was typed on stdin since the last tick, unless a whole line already waits to be sent."""
        assert self._input.stdin is not None
        try:
            fd = self._input.stdin.fileno()
            if b"\n" not in self._input.typed and select.select([fd], [], [], 0)[0]:
                self._input.typed += os.read(fd, 4096)
        except (OSError, ValueError):
            return

    def _line(self) -> str:
        """Returns the next whole line taken in from stdin, or an empty text when none is complete yet."""
        line, newline, rest = self._input.typed.partition(b"\n")
        if not newline:
            return ""
        self._input.typed = rest
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
        tree = render_tree(journal, run_id, now, self._reference, phase_timed=self._out.isatty())
        shown = tree if self._out.isatty() else render_tree(journal, run_id, now, self._reference, timed=False)
        if shown == self._last:
            self._report(journal, run_id)
            return
        if self._out.isatty():
            columns = max(shutil.get_terminal_size().columns, 2)
            if self._last:
                self._out.write(f"\x1b[{_screen_rows(self._drawn, columns)}F\x1b[J")
            # Clipped short of the last column, so no row wraps and the next redraw knows the rows it moves up.
            tree = "".join(line[: columns - 1] + "\n" for line in tree.split("\n")[:-1])
            self._drawn = tree
        self._last = shown
        self._out.write(tree)
        self._out.flush()
        self._report(journal, run_id)

    def tick(self, journal: Journal, run_id: str) -> None:
        """Redraws the tree of a TTY on a tick without events, so the time moves."""
        if self._out.isatty():
            self.show(journal, run_id)
