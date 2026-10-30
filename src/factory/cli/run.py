"""`factory run` starts a Run of a Spec, and drives it to its end."""

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path

from factory.artifacts import RunDir
from factory.cli.common import (
    HOST_TOOL_ERRORS,
    ports_of,
    preflight,
    report,
    sigterm_as_exit,
    tell_orphaned,
    took,
    tracker_of,
)
from factory.config import load_project, load_slots
from factory.fingerprint import roles
from factory.git import HostGit
from factory.journal import Journal
from factory.names import integration_branch
from factory.orchestrator import Orchestrator, new_run_id, resolve_base
from factory.run_view import RunView
from factory.sleep_block import SleepBlock
from factory.states import RunState
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
    git = HostGit(project.repo)
    base_commit = resolve_base(git, project.base, tracker.pull_requests)
    if base_commit is None:
        print(f"factory: the Run pushes to no git remote, so it starts from the local {project.base}", file=sys.stderr)
        base_commit = git.resolve(project.base)
    journal = Journal(machine.journal_path, machine.clock)
    run_id = new_run_id(machine.clock)
    print(f"Run {run_id} started on {integration_branch(run_id)}", flush=True)
    assert project.library is not None
    frozen = RunDir(machine.home / "runs" / run_id).library
    project.library.freeze(roles(project.pipeline), project.pipeline.path, frozen)
    orchestrator = Orchestrator(
        journal, project, ports, machine.home, RunView(sys.stdout, machine.clock, machine.stdin), slots
    )
    return drive(
        orchestrator,
        journal,
        lambda: orchestrator.start(run_id, snapshot, tracker, base_commit),
        machine,
        args.no_prevent_sleep,
    )


def drive(
    orchestrator: Orchestrator, journal: Journal, begin: Callable[[], None], machine: Machine, no_prevent_sleep: bool
) -> int:
    """Begins the Run, ticks it to its end and prints how it ended, and returns the exit code.

    Unless the project file or `no_prevent_sleep` turns it off, the computer stays awake while the Run goes,
    and the block is released when it ends.
    """
    block = SleepBlock([])
    try:
        with sigterm_as_exit():
            begin()
            if orchestrator.project.prevent_sleep and not no_prevent_sleep:
                block = SleepBlock.acquire(machine.platform, os.getpid())
                orchestrator.journal_sleep_block(block)
                if block.reason is not None:
                    print(
                        f"factory: warning: the computer may sleep while the Run goes: {block.reason}", file=sys.stderr
                    )
            state = orchestrator.run()
            if machine.audit is not None:
                machine.audit(orchestrator)
            ran = took(journal, orchestrator.run_id, machine.clock)
    except (KeyboardInterrupt, SystemExit) as error:
        # Ctrl-C and `kill` leave the Run orphaned, so its slots go back to the machine and `finish` can end it.
        orchestrator.interrupt(str(error) or type(error).__name__)
        raise
    except HOST_TOOL_ERRORS as error:
        # A host tool the owner can fix leaves the Run orphaned, so `resume` goes on once the host works again.
        if not orchestrator.started:
            raise
        orchestrator.interrupt(str(error))
        tell_orphaned(orchestrator.run_id, error)
        return 2
    except BaseException as error:
        orchestrator.abort(str(error) or type(error).__name__)
        raise
    finally:
        block.release()
        journal.close()
    report(orchestrator, state, ran)
    return 0 if state == RunState.DONE else 1
