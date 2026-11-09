"""`factory status` shows a Run from the Journal."""

import argparse
import json
import shlex
import sys
from typing import Any

from factory.core.events import Event
from factory.core.states import OOM, OOM_ADVICE
from factory.journal.custody import RunCustody
from factory.journal.journal import EventRow, Journal
from factory.ports.tracker import SpecLink
from factory.views.run_view import (
    budget_note,
    format_duration,
    format_spend,
    pause_note,
    phase_note,
    run_node,
    run_phase,
    run_seconds,
    ticket_seconds,
    waits,
)
from factory.views.timeline import timeline
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    status = commands.add_parser("status", help="show a Run from the journal")
    status.add_argument("run_id")
    status.add_argument("--timeline", action="store_true", help="list the nodes the Run entered, in order")
    status.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    journal = Journal(machine.journal_path, machine.clock)
    try:
        RunCustody(journal, machine.clock).orphan_stale()
        run = journal.run(args.run_id)
        if run is None:
            print(f"factory: no Run {args.run_id}", file=sys.stderr)
            return 2
        if args.timeline:
            print("\n".join(timeline(journal.events(run.id))))
            return 0
        now = machine.clock.now()
        took = format_duration(run_seconds(run, now))
        print(
            f"Run {run.id}  {run.state}  {run.integration_branch}{pause_note(run)}  {took}  {format_spend(run.spend)}"
        )
        tickets = journal.tickets(run.id)
        rows = [("TICKET", "NODE", "LAST STAGE", "ROUND", "ATTEMPT", "LOST", "STATE", "DURATION", "COST")]
        for t in tickets:
            ran = format_duration(ticket_seconds(t, now)) if t.claimed_at else "-"
            rows.append(
                (
                    t.id,
                    t.node or "-",
                    t.last_stage or "-",
                    str(t.round),
                    str(t.attempt),
                    str(t.sandbox_losses),
                    t.state,
                    ran,
                    format_spend(t.spend),
                )
            )
        widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
        for row in rows:
            print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
        events = journal.events(run.id)
        phase = run_phase(run, events)
        if phase != run.state:
            print(f"Run phase: {phase}")
        if (node := run_node(run, events)) is not None:
            print(f"Run node: {node}")
        if (note := phase_note(run, events, now)) is not None:
            print(note)
        if (budget := budget_note(run, events)) is not None:
            print(budget)
        if unlike := _unlike_file(events[0].payload):
            print(f"Settings unlike the project file: {unlike}")
        reference = machine.tracker_of(SpecLink(run.spec_ref)).closing.reference
        for ticket, wait in waits(tickets, events, reference).items():
            print(f"Ticket {ticket} {wait}")
        ooms = _ooms(events)
        removed = _removed_ignored(events)
        for t in tickets:
            if t.last_loss:
                print(f"Ticket {t.id} lost its Sandbox {t.sandbox_losses}x, last: {t.last_loss}")
            if t.id in ooms:
                print(f"Ticket {t.id} ran out of memory in its Sandbox{ooms[t.id]}: {OOM_ADVICE}")
            if t.id in removed:
                print(f"Ticket {t.id} had ignored files removed: {', '.join(removed[t.id])}")
            if t.kept_ref:
                print(f"Ticket {t.id} abandoned, its work is in {t.kept_ref}")
        if by_hand := _by_hand(events):
            print("Land it by hand:")
            for argv in by_hand:
                print(f"  {shlex.join(argv)}")
    finally:
        journal.close()
    return 0


def _unlike_file(started: dict[str, Any]) -> str:
    """Returns the settings the Run recorded that the project file alone does not give, as `--set` would write them."""
    settings = started.get("settings", {})
    return ", ".join(
        f"{key}={_shown(settings[key])}" for key in started.get("settings_unlike_file", []) if key in settings
    )


def _shown(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _by_hand(events: list[EventRow]) -> list[list[str]]:
    """Returns the commands that finish a landing that failed, until a later landing opened the top PR."""
    commands: list[list[str]] = []
    for event in events:
        if event.type == Event.LandingFailed:
            commands = event.payload.get("commands", [])
        elif event.type in (Event.PullRequestOpened, Event.PullRequestRecorded) and event.payload.get("top", True):
            commands = []
    return commands


def _removed_ignored(events: list[EventRow]) -> dict[str, list[str]]:
    """Returns the ignored files the host removed after the done Attempts of each Ticket, in the order they went."""
    found: dict[str, list[str]] = {}
    for event in events:
        if event.type == Event.AttemptFinished and event.payload.get("outcome") == "done":
            for name in event.payload.get("removed_ignored", []):
                names = found.setdefault(event.payload["ticket"], [])
                if name not in names:
                    names.append(name)
    return found


def _ooms(events: list[EventRow]) -> dict[str, str]:
    """Returns the Tickets whose last failed Attempt the OOM killer killed, with the Sandbox's limit and peak."""
    found: dict[str, str] = {}
    for event in events:
        if event.type != Event.AttemptFinished:
            continue
        ticket = event.payload["ticket"]
        if event.payload.get("cause") != OOM:
            found.pop(ticket, None)
            continue
        facts = [f"limit {event.payload['memory_limit']}"] if "memory_limit" in event.payload else []
        if "memory_peak" in event.payload:
            facts.append(f"peak {event.payload['memory_peak']} bytes")
        found[ticket] = f" ({', '.join(facts)})" if facts else ""
    return found
