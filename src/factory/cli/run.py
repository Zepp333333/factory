"""`factory run` starts a Run of a Spec, and drives it to its end."""

import argparse
import sys
from pathlib import Path

from factory.cli.common import add_set, checked, drive, invocation_of, project_of, warn
from factory.config import load_slots
from factory.ports.tracker import SpecLink, Tracker, TrackerError
from factory.run.run_entry import new_run
from factory.views.run_view import RunView
from factory.wiring import Machine


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


def _tracker(link: SpecLink, machine: Machine) -> Tracker:
    if link.tickets is not None and not machine.tracker_owned(link.spec):
        raise TrackerError("--tickets takes sub-issues of a GitHub Spec, not Tickets of a folder")
    return machine.tracker_of(link)


def command(args: argparse.Namespace, machine: Machine) -> int:
    project = project_of(args.project, machine, invocation=invocation_of(args.settings))
    link = SpecLink(args.spec, args.tickets)
    tracker = _tracker(link, machine)
    warnings = checked(project, machine, link)
    ports = machine.ports_of(project)
    slots = load_slots(machine.config_path)
    snapshot = tracker.read_spec()
    warn(warnings)
    pull_requests = machine.pull_requests_of(project, tracker)
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
        no_prevent_sleep=False,
    )
