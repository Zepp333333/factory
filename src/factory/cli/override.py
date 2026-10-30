"""`factory override` replaces the outcome of a Decision."""

import argparse

from factory.cli.common import send_command
from factory.commands import Override
from factory.journal import Journal
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    override = commands.add_parser("override", help="replace the outcome of the current or last Decision of a Ticket")
    override.add_argument("run_id")
    override.add_argument("ticket")
    override.add_argument("outcome")
    override.add_argument("--reason", action="append", default=[], help="why; required to send the Ticket back to work")
    override.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    return send_command(
        Journal(machine.journal_path, machine.clock),
        machine,
        args.run_id,
        Override(args.ticket, args.outcome, args.reason),
    )
