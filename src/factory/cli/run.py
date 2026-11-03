"""`factory run` starts a Run of a Spec, and drives it to its end."""

import argparse
import sys
from pathlib import Path

from factory.cli.common import add_set, checked, drive, invocation_of, project_of, warn
from factory.config import load_slots
from factory.ports.tracker import SpecLink
from factory.run.run_entry import new_run
from factory.views.run_view import RunView
from factory.wiring import Machine


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]", trackers: tuple[str, ...]) -> None:
    run = commands.add_parser("run", help="run a Spec through the Pipeline")
    run.add_argument("spec", help=f"the link or folder of the Spec, which a Tracker kind takes: {', '.join(trackers)}")
    run.add_argument(
        "--tickets", type=_ticket_ids, help="comma-separated ids of the Tickets of the Spec to take, such as 28,30"
    )
    run.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    run.add_argument(
        "--no-prevent-sleep",
        dest="settings",
        action="append_const",
        const={"prevent_sleep": False},
        default=[],
        help="let the computer sleep when idle while the Run goes, as --set prevent_sleep=false",
    )
    add_set(run)
    run.set_defaults(command=command)


def _ticket_ids(text: str) -> list[str]:
    """Splits the ids at commas; the kind of the Spec's Tracker checks them."""
    ids = [item.strip() for item in text.split(",")]
    if not all(ids):
        raise argparse.ArgumentTypeError("must be Ticket ids separated by commas, such as 28,30")
    return ids


def command(args: argparse.Namespace, machine: Machine) -> int:
    project = project_of(args.project, machine, invocation=invocation_of(args.settings))
    link = SpecLink(args.spec, args.tickets)
    warnings = checked(project, machine, link)
    tracker = machine.tracker_of(link)
    ports = machine.ports_of(project)
    slots = load_slots(machine.config_path)
    snapshot = tracker.spec.read_spec()
    warn(warnings)
    pull_requests = machine.pull_requests_of(project, tracker.spec)
    orchestrator = new_run(
        project,
        tracker,
        snapshot,
        ports,
        pull_requests,
        machine.home,
        machine.journal_path,
        RunView(sys.stdout, machine.clock, tracker.closing.reference, machine.stdin),
        slots,
    )
    return drive(
        orchestrator,
        orchestrator.journal,
        orchestrator.start,
        machine,
        no_prevent_sleep=False,
    )
