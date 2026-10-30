"""`factory resume` continues an orphaned Run."""

import argparse

from factory.cli.common import drive, take_orphaned
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    resume = commands.add_parser("resume", help="continue an orphaned Run from the journal")
    resume.add_argument("run_id")
    resume.add_argument(
        "--no-prevent-sleep", action="store_true", help="let the computer sleep when idle while the Run goes"
    )
    resume.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    """Continues an orphaned Run, or a running one whose heartbeat is stale, from its events in the journal."""
    taken = take_orphaned(args.run_id, machine, "resumes")
    if isinstance(taken, int):
        return taken
    journal, run, orchestrator, tracker = taken
    print(f"Run {run.id} resumed on {run.integration_branch}", flush=True)
    return drive(
        orchestrator,
        journal,
        lambda: orchestrator.resume(run.id, tracker),
        machine,
        args.no_prevent_sleep,
    )
