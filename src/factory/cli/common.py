"""What several commands share: host tool errors, the Tracker of a Spec, the ports of a Run, an orphaned Run taken from
the Journal, and driving a Run to its end."""

import argparse
import os
import shlex
import signal
import sys
import threading
from collections.abc import Callable, Iterator
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
from factory.run_context import RunContext
from factory.run_view import RunView, format_duration, format_spend, run_seconds
from factory.sandbox import SandboxError
from factory.sleep_block import SleepBlock
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
    sandbox = machine.sandbox_of(project)
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


def take_orphaned(run_id: str, machine: Machine, verb: str) -> tuple[Journal, RunRow, Orchestrator] | int:
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
            machine.pull_requests_of(project, run.spec_ref, tracker),
            RunContext.replayed(project, machine.home, run.id, tracker, journal.events(run.id)),
        )
    except BaseException:
        journal.close()
        raise
    return journal, run, orchestrator


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
