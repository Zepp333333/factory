"""`factory run` starts a Run of a Spec, and drives it to its end."""

import argparse
import sys
from pathlib import Path

from factory.cli.common import drive, ports_of, preflight, tracker_of
from factory.config import load_project, load_slots
from factory.git import HostGit
from factory.journal import Journal
from factory.names import integration_branch
from factory.orchestrator import Orchestrator
from factory.run_start import freeze_library, new_run_id, resolve_base
from factory.run_view import RunView
from factory.tracker import Tracker, TrackerError
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
        "--no-prevent-sleep", action="store_true", help="let the computer sleep when idle while the Run goes"
    )
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
    project = load_project(args.project, machine.library, machine.kinds)
    tracker = _tracker(args, machine)
    preflight(args.spec, tracker, project, machine)
    ports = ports_of(project, machine)
    slots = load_slots(machine.config_path)
    snapshot = tracker.read_spec()
    warnings = machine.tracker_kind(args.spec).warnings
    for warning in warnings(tracker, project.base) if warnings else []:
        print(f"factory: warning: {warning}", file=sys.stderr)
    pull_requests = machine.pull_requests_of(project, args.spec, tracker)
    git = HostGit(project.repo)
    base_commit = resolve_base(git, project.base, pull_requests)
    if base_commit is None:
        print(f"factory: the Run pushes to no git remote, so it starts from the local {project.base}", file=sys.stderr)
        base_commit = git.resolve(project.base)
    journal = Journal(machine.journal_path, machine.clock)
    run_id = new_run_id(machine.clock)
    print(f"Run {run_id} started on {integration_branch(run_id)}", flush=True)
    freeze_library(project, machine.home, run_id)
    orchestrator = Orchestrator(
        journal, project, ports, machine.home, RunView(sys.stdout, machine.clock, machine.stdin), slots, pull_requests
    )
    return drive(
        orchestrator,
        journal,
        lambda: orchestrator.start(run_id, snapshot, tracker, base_commit),
        machine,
        args.no_prevent_sleep,
    )
