"""What several commands share: host tool errors, the Tracker of a Spec, an orphaned Run taken from the Journal,
and driving a Run to its end."""

import argparse
import shlex
import signal
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any, NoReturn

from factory.config import ConfigError, Project, Sources, load_defaults, load_project, load_slots, merged, parse_set
from factory.core.git import GitError, host_env
from factory.core.git import run as run_git
from factory.core.states import RunState
from factory.journal.commands import Command, send
from factory.journal.custody import RunCustody
from factory.journal.journal import Journal, RunRow
from factory.ports.sandbox import SandboxError
from factory.ports.tracker import SpecLink, TrackerError
from factory.ports.workbench import WorkbenchError
from factory.run.run_entry import Orchestrator, keep_awake, replayed_run
from factory.views.run_view import RunView, format_duration, format_spend, run_seconds
from factory.wiring import Machine

# A failure of one of these is a host tool the owner can fix, such as `git` or the CLI of a code host.
HOST_TOOL_ERRORS = (GitError, WorkbenchError, SandboxError, TrackerError)


def add_set(parser: argparse.ArgumentParser) -> None:
    """Adds `--set key=value`, which overrides a key of the project file for this invocation."""
    parser.add_argument(
        "--set",
        dest="settings",
        metavar="KEY=VALUE",
        type=_setting,
        action="append",
        default=[],
        help="override a key of the project file, with dots into blocks, such as landing=stack or ci.timeout=30m",
    )


def _setting(text: str) -> dict[str, Any]:
    try:
        return parse_set(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def invocation_of(settings: list[dict[str, Any]]) -> dict[str, Any]:
    """Lays the `--set` of an invocation over one another, a later one winning."""
    invocation: dict[str, Any] = {}
    for setting in settings:
        invocation = merged(invocation, setting)
    return invocation


def repo_of(project: Path) -> Path:
    """Returns the repository of the project file, or the directory it would sit in outside of one."""
    directory = project.resolve().parent
    try:
        return Path(run_git(["rev-parse", "--show-toplevel"], host_env(), cwd=directory))
    except (GitError, OSError):
        return directory


def project_of(
    path: Path,
    machine: Machine,
    invocation: Mapping[str, Any] | None = None,
    recorded: Mapping[str, Any] | None = None,
    run_id: str | None = None,
) -> Project:
    """Loads the project file over the Factory config, under the invocation and what a Run recorded.

    The project of the Run `run_id` takes its Library from what the Run froze.
    """
    sources = Sources(load_defaults(machine.config_path), invocation or {}, recorded)
    library_for = machine.library if run_id is None else machine.run_library(run_id)
    return load_project(path, library_for, machine.kinds, sources)


def recorded_project(journal: Journal, run_id: str, machine: Machine) -> Project:
    """Loads the project of a Run with the settings its `RunStarted` recorded, or from the project file
    for a Run from before they were recorded."""
    started = journal.events(run_id)[0].payload
    return project_of(Path(started["project"]), machine, recorded=started.get("settings"), run_id=run_id)


def checked(project: Project, machine: Machine, link: SpecLink | None = None) -> tuple[str, ...]:
    """Refuses to go on when a kind the project file picks, or the kind of the Spec's Tracker, finds an error in its
    settings, and returns the warnings they find."""
    problems = machine.problems(project, link)
    if problems.errors:
        raise ConfigError(*problems.errors)
    return problems.warnings


def warn(warnings: tuple[str, ...]) -> None:
    for warning in warnings:
        print(f"factory: warning: {warning}", file=sys.stderr)


def tell_orphaned(run_id: str, error: Exception) -> None:
    """Names the host tool call that failed and the commands that continue or end the orphaned Run."""
    print(f"factory: {error}", file=sys.stderr)
    print(f"Run {run_id} is orphaned. Fix the host, then continue the Run or end it:", file=sys.stderr)
    print(f"factory resume {run_id}", file=sys.stderr)
    print(f"factory finish {run_id}", file=sys.stderr)


def took(journal: Journal, run_id: str, machine: Machine) -> str:
    """Returns how long the Run went and what its Attempts cost, such as `41m 12s, $3.20`.

    A Run that ends without `RunFinished`, such as a paused one, has gone until now.
    """
    run = journal.run(run_id)
    assert run is not None
    return f"{format_duration(run_seconds(run, machine.clock.now()))}, {format_spend(run.spend)}"


def report(orchestrator: Orchestrator, state: RunState, took: str) -> None:
    """Prints how the Run ended, its pull request and the commands that land it by hand."""
    ci = f", {orchestrator.ci_note}" if orchestrator.ci_note else ""
    print(f"Run {orchestrator.run_id} finished: {state} in {took}{ci}")
    if orchestrator.pull_request:
        merge = f", {orchestrator.merge_note}" if orchestrator.merge_note else ""
        print(f"Pull request: {orchestrator.pull_request}{merge}")
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
        project = recorded_project(journal, run.id, machine)
        tracker = machine.tracker_of(SpecLink(run.spec_ref))
        ports = machine.ports_of(project)
        slots = load_slots(machine.config_path)
        orchestrator = replayed_run(
            journal,
            run.id,
            project,
            tracker,
            ports,
            machine.pull_requests_of(project, tracker.spec),
            machine.home,
            RunView(sys.stdout, machine.clock, tracker.closing.reference, machine.owner_input, ports.workbench),
            slots,
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
    block = None
    try:
        with sigterm_as_exit():
            begin()
            if orchestrator.project.prevent_sleep and not no_prevent_sleep:
                block = keep_awake(orchestrator, machine.platform)
                if block.reason is not None:
                    print(
                        f"factory: warning: the computer may sleep while the Run goes: {block.reason}", file=sys.stderr
                    )
            state = orchestrator.run()
            if machine.audit is not None:
                machine.audit(orchestrator)
            ran = took(journal, orchestrator.run_id, machine)
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
        if block is not None:
            block.release()
        journal.close()
    report(orchestrator, state, ran)
    return 0 if state == RunState.DONE else 1
