"""`factory cancel` abandons a Ticket."""

import argparse

from factory.cli.common import send_command
from factory.commands import Cancel
from factory.journal import Journal
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    cancel = commands.add_parser(
        "cancel", help="abandon a Ticket, which keeps the Tickets that depend on it from starting"
    )
    cancel.add_argument("run_id")
    cancel.add_argument("ticket")
    cancel.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    return send_command(Journal(machine.journal_path, machine.clock), machine, args.run_id, Cancel(args.ticket))
