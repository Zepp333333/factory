"""`factory status` shows a Run from the Journal."""

import argparse
import sys

from factory.custody import RunCustody
from factory.events import Event
from factory.journal import EventRow, Journal
from factory.run_view import (
    ci_wait,
    format_duration,
    format_spend,
    pause_note,
    run_phase,
    run_seconds,
    ticket_seconds,
    waits,
)
from factory.states import OOM, OOM_ADVICE
from factory.timeline import timeline
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
        if (ci := ci_wait(run, events)) is not None:
            print(f"Run {ci}")
        for ticket, wait in waits(tickets, events).items():
            print(f"Ticket {ticket} {wait}")
        ooms = _ooms(events)
        for t in tickets:
            if t.last_loss:
                print(f"Ticket {t.id} lost its Sandbox {t.sandbox_losses}x, last: {t.last_loss}")
            if t.id in ooms:
                print(f"Ticket {t.id} ran out of memory in its Sandbox{ooms[t.id]}: {OOM_ADVICE}")
            if t.kept_ref:
                print(f"Ticket {t.id} abandoned, its work is in {t.kept_ref}")
    finally:
        journal.close()
    return 0


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
