"""`factory decide` answers the Decision of a Ticket that waits for you."""

import argparse

from factory.cli.common import send_command
from factory.commands import decide_for
from factory.journal import Journal
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    decide = commands.add_parser("decide", help="answer the Decision of a Ticket that waits for you")
    decide.add_argument("run_id")
    decide.add_argument("ticket")
    decide.add_argument("outcome")
    decide.add_argument("--reason", action="append", default=[], help="why; required to send the Ticket back to work")
    decide.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    journal = Journal(machine.journal_path, machine.clock)
    try:
        run = journal.run(args.run_id)
        waiting = next((w for w in journal.waiting(args.run_id) if w.ticket == args.ticket), None) if run else None
    except BaseException:
        journal.close()
        raise
    return send_command(
        journal, machine, args.run_id, decide_for(args.ticket, args.outcome, args.reason, "cli", waiting)
    )
