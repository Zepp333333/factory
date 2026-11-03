"""`factory run` starts a Run of a Spec, and drives it to its end."""

import argparse
import sys
from pathlib import Path

from factory.cli.common import add_set, drive, invocation_of, preflight, project_of, tracker_of
from factory.config import load_slots
from factory.ports.tracker import Tracker, TrackerError
from factory.run.run_entry import new_run
from factory.views.run_view import RunView
from factory.wiring import Machine, tracker_owned


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    run = commands.add_parser("run", help="run a Spec through the Pipeline")
    run.add_argument(
        "spec", help="link to the GitHub or GitLab issue of the Spec, or the Spec folder of a LocalTracker"
    )
    run.add_argument(
        "--tickets", type=_issue_numbers, help="comma-separated sub-issues or child Tasks of a Spec to take"
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


def _issue_numbers(text: str) -> list[int]:
    try:
        return [int(item) for item in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("must be issue numbers such as 28,30") from None


def _tracker(args: argparse.Namespace, machine: Machine) -> Tracker:
    if args.tickets is not None and not tracker_owned(args.spec):
        raise TrackerError("--tickets takes sub-issues of a GitHub Spec, not Tickets of a folder")
    return tracker_of(args.spec, args.tickets, machine)


def command(args: argparse.Namespace, machine: Machine) -> int:
    project = project_of(args.project, machine, invocation=invocation_of(args.settings))
    tracker = _tracker(args, machine)
    preflight(args.spec, tracker, project, machine)
    ports = machine.ports_of(project)
    slots = load_slots(machine.config_path)
    snapshot = tracker.read_spec()
    warnings = machine.tracker_kind(args.spec).warnings
    for warning in warnings(tracker, project.base) if warnings else []:
        print(f"factory: warning: {warning}", file=sys.stderr)
    pull_requests = machine.pull_requests_of(project, args.spec, tracker)
    orchestrator = new_run(
        project,
        tracker,
        snapshot,
        ports,
        pull_requests,
        machine.home,
        machine.journal_path,
        RunView(sys.stdout, machine.clock, machine.stdin),
        slots,
    )
    return drive(
        orchestrator,
        orchestrator.journal,
        orchestrator.start,
        machine,
        False,
    )
