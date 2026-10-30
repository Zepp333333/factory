"""`factory attach` follows the terminal of the latest Attempt of a Ticket."""

import argparse
import sys

from factory.journal import Journal
from factory.states import RunState
from factory.wiring import Machine
from factory.workbench import TERMINAL_LOG, marker

# `attach` looks this often, in seconds, for new lines in the terminal log of the Attempt.
ATTACH_POLL_SECONDS = 0.1


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    attach = commands.add_parser("attach", help="follow the terminal of the latest Attempt of a Ticket")
    attach.add_argument("run_id")
    attach.add_argument("ticket")
    attach.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    """Prints the terminal log of the Ticket's latest Attempt and follows it until the Attempt ends.

    It also stops when the Run is no longer running, because no renderer writes to the log then.
    """
    journal = Journal(machine.journal_path, machine.clock)
    try:
        run = journal.run(args.run_id)
        if run is None:
            print(f"factory: no Run {args.run_id}", file=sys.stderr)
            return 2
        logs = list((machine.home / "runs" / run.id / "tickets" / args.ticket).rglob(TERMINAL_LOG))
        if not logs:
            print(f"factory: Ticket {args.ticket} of Run {run.id} has no Attempt to attach to", file=sys.stderr)
            return 2
        log = max(logs, key=lambda path: path.stat().st_mtime_ns)
        offset = 0
        while True:
            current = journal.run(run.id)
            ended = marker(log, "done").exists() or current is None or current.state != RunState.RUNNING
            with log.open("rb") as stream:
                stream.seek(offset)
                data = stream.read()
            offset += len(data)
            print(data.decode(errors="replace"), end="", flush=True)
            if ended and not data:
                return 0
            machine.clock.sleep(ATTACH_POLL_SECONDS)
    finally:
        journal.close()
