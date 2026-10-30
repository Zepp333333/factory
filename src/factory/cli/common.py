"""What several commands share: host tool errors, the Tracker of a Spec, and an orphaned Run taken from the Journal."""

import argparse
import shlex
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import NoReturn

from factory.attempt import Ports
from factory.clock import Clock
from factory.commands import Command, send
from factory.config import ConfigError, Project, load_project, load_slots
from factory.custody import RunCustody
from factory.git import GitError, HostGit
from factory.journal import Journal, RunRow
from factory.orchestrator import Orchestrator
from factory.run_view import RunView, format_duration, format_spend, run_seconds
from factory.sandbox import SandboxError
from factory.states import RunState
from factory.tracker import Tracker, TrackerError
from factory.wiring import Machine
from factory.workbench import WorkbenchError

# A failure of one of these is a host tool the owner can fix, such as `git`, `gh`, `sbx` or `orca`.
HOST_TOOL_ERRORS = (GitError, WorkbenchError, SandboxError, TrackerError)


def tracker_of(spec: str, tickets: list[int] | None, machine: Machine) -> Tracker:
    """Picks the Tracker from the Spec's link, through the Tracker registry."""
    return machine.tracker_of(spec, tickets)


def preflight(spec: str, tracker: Tracker, project: Project, machine: Machine) -> None:
    """Refuses to go on when the Tracker's CLI cannot serve a Run, and names the fix of each problem."""
    check = machine.tracker_kind(spec).preflight
    if check is not None:
        problems = check(tracker, HostGit(project.repo).remotes(), project.repo)
        if problems:
            raise ConfigError(*problems)


def ports_of(project: Project, machine: Machine) -> Ports:
    """Builds the ports of a Run from the project file, after the Sandbox checks itself."""
    sandbox = machine.sandbox_for(project.sandbox)
    sandbox.check()
    return Ports(
        executor=machine.executor_for(project.executor),
        sandbox=sandbox,
        workbench=machine.workbench_for(project.workbench),
        clock=machine.clock,
    )


def tell_orphaned(run_id: str, error: Exception) -> None:
    """Names the host tool call that failed and the commands that continue or end the orphaned Run."""
    print(f"factory: {error}", file=sys.stderr)
    print(f"Run {run_id} is orphaned. Fix the host, then continue the Run or end it:", file=sys.stderr)
    print(f"factory resume {run_id}", file=sys.stderr)
    print(f"factory finish {run_id}", file=sys.stderr)


def took(journal: Journal, run_id: str, clock: Clock) -> str:
    """Returns how long the Run went and what its Attempts cost, such as `41m 12s, $3.20`.

    A Run that ends without `RunFinished`, such as a paused one, has gone until now.
    """
    run = journal.run(run_id)
    assert run is not None
    return f"{format_duration(run_seconds(run, clock.now()))}, {format_spend(run.spend)}"


def report(orchestrator: Orchestrator, state: RunState, took: str) -> None:
    """Prints how the Run ended, its pull request and the commands that land it by hand."""
    ci = f", {orchestrator.ci_note}" if orchestrator.ci_note else ""
    print(f"Run {orchestrator.run_id} finished: {state} in {took}{ci}")
    if orchestrator.pull_request:
        print(f"Pull request: {orchestrator.pull_request}")
    if orchestrator.by_hand:
        print("Land it by hand:")
        for command in orchestrator.by_hand:
            print(f"  {shlex.join(command)}")


def take_orphaned(run_id: str, machine: Machine, verb: str) -> tuple[Journal, RunRow, Orchestrator, Tracker] | int:
    """Builds the Orchestrator of an orphaned Run, or prints why not and returns the exit code."""
    journal = Journal(machine.journal_path, machine.clock)
    try:
        run = journal.run(run_id)
        if run is None:
            print(f"factory: no Run {run_id}", file=sys.stderr)
            journal.close()
            return 2
        RunCustody(journal, machine.clock).orphan_stale()
        run = journal.run(run.id)
        assert run is not None
        if run.state != RunState.ORPHANED:
            print(f"factory: Run {run.id} is {run.state}, and only an orphaned Run {verb}", file=sys.stderr)
            journal.close()
            return 2
        started = journal.events(run.id)[0].payload
        project = load_project(Path(started["project"]), machine.run_library(run.id), machine.kinds)
        tracker = tracker_of(run.spec_ref, None, machine)
        orchestrator = Orchestrator(
            journal,
            project,
            ports_of(project, machine),
            machine.home,
            RunView(sys.stdout, machine.clock, machine.stdin),
            load_slots(machine.config_path),
        )
    except BaseException:
        journal.close()
        raise
    return journal, run, orchestrator, tracker


@contextmanager
def sigterm_as_exit() -> Iterator[None]:
    """Turns SIGTERM into SystemExit while a Run goes, so the Run can still journal its end.

    Only the main thread can set a signal handler, so a Run in another thread keeps the default.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGTERM, _exit)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _exit(signum: int, frame: FrameType | None) -> NoReturn:
    raise SystemExit(128 + signum)


def send_command(journal: Journal, machine: Machine, run_id: str, command: Command) -> int:
    """Sends the Command to the Run and prints how it ended, and returns the exit code."""
    try:
        sent = send(journal, RunCustody(journal, machine.clock), machine.clock, run_id, command)
    finally:
        journal.close()
    match sent.status:
        case "accepted" | "waits":
            print(sent.text)
            return 0
        case "rejected":
            print(f"factory: {sent.text}", file=sys.stderr)
            return 1
        case _:
            print(f"factory: {sent.text}", file=sys.stderr)
            return 2


def positive(text: str) -> int:
    """Parses a positive integer argument."""
    try:
        number = int(text)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number
