"""`factory finish` ends an orphaned Run."""

import argparse

from factory.cli.common import report, sigterm_as_exit, take_orphaned, took
from factory.states import TicketState
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    finish = commands.add_parser("finish", help="end an orphaned Run with the Tickets it merged and open its PR")
    finish.add_argument("run_id")
    finish.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    """Ends an orphaned Run as `partial` with the Tickets it merged, and lands the Integration branch if any merged."""
    taken = take_orphaned(args.run_id, machine, "finishes")
    if isinstance(taken, int):
        return taken
    journal, run, orchestrator, tracker = taken
    merged = any(t.state == TicketState.MERGED for t in journal.tickets(run.id))
    print(f"Run {run.id} finishing on {run.integration_branch}", flush=True)
    try:
        with sigterm_as_exit():
            state = orchestrator.finish(run.id, tracker)
            ran = took(journal, run.id, machine.clock)
    except (KeyboardInterrupt, SystemExit) as error:
        # A `finish` stopped halfway leaves the Run orphaned again, so `finish` can be run once more.
        orchestrator.interrupt(str(error) or type(error).__name__)
        raise
    finally:
        journal.close()
    report(orchestrator, state, ran)
    if not merged:
        print(f"No Ticket of Run {run.id} merged, so no pull request was opened")
    return 0
