"""`factory clean` lists and removes what Runs no longer running left."""

import argparse
from pathlib import Path

from factory.clean import Cleaner
from factory.config import ConfigError, load_project
from factory.custody import RunCustody
from factory.journal import Journal
from factory.states import RunState
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    clean = commands.add_parser(
        "clean", help="list the Workspaces, Sandboxes and branches that Runs no longer running left, and remove them"
    )
    clean.add_argument("--yes", action="store_true", help="remove them; without it only print what would go")
    clean.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    """Prints the leftovers of the Runs that are no longer running, and with `--yes` removes them.

    A Run is no longer running when it ended or its heartbeat went stale, and a Run that still runs is not touched.
    """
    journal = Journal(machine.journal_path, machine.clock)
    found = False
    try:
        RunCustody(journal, machine.clock).orphan_stale()
        for run_id in journal.run_ids():
            run = journal.run(run_id)
            assert run is not None
            if run.state == RunState.RUNNING:
                continue
            try:
                project = load_project(
                    Path(journal.events(run.id)[0].payload["project"]), machine.run_library(run.id), machine.kinds
                )
            except ConfigError as error:
                print(f"Run {run.id} ({run.state}): skipped, its project file does not load: {'; '.join(error.errors)}")
                continue
            cleaner = Cleaner(
                journal,
                machine.home,
                run.id,
                project,
                machine.sandbox_for(project.sandbox),
                machine.workbench_for(project.workbench),
            )
            for leftover in cleaner.find():
                if not found:
                    print("Leftovers of Runs that are no longer running:")
                    found = True
                print(f"Run {run.id} ({run.state}), Ticket {leftover.ticket}:")
                for line in cleaner.clean(leftover) if args.yes else cleaner.plan(leftover):
                    print(f"  {line}")
    finally:
        journal.close()
    if not found:
        print("Nothing to clean")
    elif not args.yes:
        print("Nothing was changed; run `factory clean --yes` to remove them")
    return 0
