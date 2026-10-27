"""The `factory` CLI is the only interface that starts and inspects Runs."""

import argparse
import os
import shlex
import signal
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from types import FrameType
from typing import NoReturn, TextIO

from factory.claude_code import ClaudeCodeExecutor
from factory.clock import Clock, SystemClock
from factory.commands import submit_decide
from factory.config import ConfigError, display_path, load_project, load_slots
from factory.executor import Executor
from factory.git import GitError
from factory.github import GitHubTracker, is_issue_link
from factory.journal import Journal
from factory.orchestrator import Orchestrator, Ports, integration_branch, new_run_id
from factory.render import marker
from factory.run_view import RunView, pause_note
from factory.sandbox import LocalSandbox, Sandbox, SandboxError, SandboxSettings
from factory.sbx import SbxSandbox
from factory.states import CommandState, RunState
from factory.tracker import LocalTracker, Tracker, TrackerError
from factory.workbench import TERMINAL_LOG, OrcaWorkbench, TerminalWorkbench, Workbench, WorkbenchError

# `decide` looks this often, in seconds, for the Orchestrator's mark on its Command.
DECIDE_POLL_SECONDS = 0.1

# `attach` looks this often, in seconds, for new lines in the terminal log of the Attempt.
ATTACH_POLL_SECONDS = 0.1


def make_sandbox(settings: SandboxSettings) -> Sandbox:
    return SbxSandbox(settings) if settings.kind == "sbx" else LocalSandbox()


def make_workbench(kind: str) -> Workbench:
    return OrcaWorkbench() if kind == "orca" else TerminalWorkbench()


def default_home() -> Path:
    return Path(os.environ.get("FACTORY_HOME") or Path.home() / ".local" / "share" / "software-factory")


@dataclass
class Machine:
    """Machine holds the per-machine pieces every command runs with, and tests replace it as a whole.

    `workbench` replaces the one the project file's `workbench` asks for,
    `sandbox_for` makes the Sandbox the project file's `sandbox` block asks for, `gh` is the command
    that reaches GitHub, and `stdin` is where the Run's terminal takes the owner's answers when it is a TTY.
    The factory config in `config.yaml` of `home` caps the Attempts that all Runs on the machine have going at once.
    """

    home: Path = field(default_factory=default_home)
    clock: Clock = field(default_factory=SystemClock)
    executor: Executor = field(default_factory=ClaudeCodeExecutor)
    sandbox_for: Callable[[SandboxSettings], Sandbox] = make_sandbox
    workbench: Workbench | None = None
    gh: Sequence[str] = ("gh",)
    stdin: TextIO = field(default_factory=lambda: sys.stdin)

    @property
    def config_path(self) -> Path:
        return self.home / "config.yaml"

    @property
    def journal_path(self) -> Path:
        return self.home / "factory.db"


def main(argv: list[str] | None = None, machine: Machine | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exit:
        return exit.code if isinstance(exit.code, int) else 1
    machine = machine or Machine()
    try:
        code: int = args.command(args, machine)
    except (ConfigError, TrackerError, GitError, WorkbenchError, SandboxError) as error:
        for message in error.errors if isinstance(error, ConfigError) else [str(error)]:
            print(f"factory: {message}", file=sys.stderr)
        return 2
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory")
    parser.add_argument("--version", action="version", version=f"factory {version('software-factory')}")
    commands = parser.add_subparsers(required=True)

    run = commands.add_parser("run", help="run a Spec through the Pipeline")
    run.add_argument("spec", help="link to the GitHub issue of the Spec, or the Spec folder of a LocalTracker")
    run.add_argument("--tickets", type=_issue_numbers, help="comma-separated sub-issues of a GitHub Spec to take")
    run.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    run.set_defaults(command=_run)

    check = commands.add_parser("check", help="check the project file and its Pipeline before a Run")
    check.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    check.set_defaults(command=_check)

    status = commands.add_parser("status", help="show a Run from the journal")
    status.add_argument("run_id")
    status.set_defaults(command=_status)

    decide = commands.add_parser("decide", help="answer the Decision of a Ticket that waits for you")
    decide.add_argument("run_id")
    decide.add_argument("ticket")
    decide.add_argument("outcome")
    decide.add_argument("--reason", action="append", default=[], help="why; required to send the Ticket back to work")
    decide.set_defaults(command=_decide)

    attach = commands.add_parser("attach", help="follow the terminal of the latest Attempt of a Ticket")
    attach.add_argument("run_id")
    attach.add_argument("ticket")
    attach.set_defaults(command=_attach)
    return parser


def _issue_numbers(text: str) -> list[int]:
    try:
        return [int(item) for item in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("must be issue numbers such as 28,30") from None


def _tracker(args: argparse.Namespace, machine: Machine) -> Tracker:
    if is_issue_link(args.spec):
        return GitHubTracker(args.spec, args.tickets, command=machine.gh)
    if args.tickets is not None:
        raise TrackerError("--tickets takes sub-issues of a GitHub Spec, not Tickets of a folder")
    return LocalTracker(Path(args.spec))


def _run(args: argparse.Namespace, machine: Machine) -> int:
    project = load_project(args.project)
    tracker = _tracker(args, machine)
    sandbox = machine.sandbox_for(project.sandbox)
    sandbox.check()
    slots = load_slots(machine.config_path)
    snapshot = tracker.read_spec()
    ports = Ports(
        executor=machine.executor,
        sandbox=sandbox,
        workbench=machine.workbench or make_workbench(project.workbench),
        clock=machine.clock,
    )
    journal = Journal(machine.journal_path, machine.clock)
    run_id = new_run_id(machine.clock)
    print(f"Run {run_id} started on {integration_branch(run_id)}", flush=True)
    orchestrator = Orchestrator(journal, project, ports, machine.home, RunView(sys.stdout, machine.stdin), slots)
    try:
        with _sigterm_as_exit():
            orchestrator.start(run_id, snapshot, tracker)
            state = orchestrator.run()
    except BaseException as error:
        # Ctrl-C and `kill` end the Run as `aborted` too, so its slots go back to the machine.
        orchestrator.abort(str(error) or type(error).__name__)
        raise
    finally:
        journal.close()
    print(f"Run {run_id} finished: {state}")
    if orchestrator.pull_request:
        print(f"Pull request: {orchestrator.pull_request}")
    if orchestrator.by_hand:
        print("Land it by hand:")
        for command in orchestrator.by_hand:
            print(f"  {shlex.join(command)}")
    return 0 if state == RunState.DONE else 1


@contextmanager
def _sigterm_as_exit() -> Iterator[None]:
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


def _check(args: argparse.Namespace, machine: Machine) -> int:
    project = load_project(args.project)
    print(f"{display_path(project.path)}: OK")
    return 0


def _status(args: argparse.Namespace, machine: Machine) -> int:
    journal = Journal(machine.journal_path, machine.clock)
    try:
        run = journal.run(args.run_id)
        if run is None:
            print(f"factory: no Run {args.run_id}", file=sys.stderr)
            return 2
        print(f"Run {run.id}  {run.state}  {run.integration_branch}{pause_note(run)}")
        rows = [("TICKET", "NODE", "LAST STAGE", "ROUND", "ATTEMPT", "LOST", "STATE")]
        for t in journal.tickets(run.id):
            rows.append(
                (t.id, t.node or "-", t.last_stage or "-", str(t.round), str(t.attempt), str(t.sandbox_losses), t.state)
            )
        widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
        for row in rows:
            print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
        for t in journal.tickets(run.id):
            if t.kept_ref:
                print(f"Ticket {t.id} abandoned, its work is in {t.kept_ref}")
    finally:
        journal.close()
    return 0


def _decide(args: argparse.Namespace, machine: Machine) -> int:
    """Puts the answer in the Run's inbox and waits until its Orchestrator accepts or rejects it.

    The Command stays in the inbox if the wait is interrupted, and the Run applies it on its next tick.
    """
    journal = Journal(machine.journal_path, machine.clock)
    try:
        run = journal.run(args.run_id)
        if run is None:
            print(f"factory: no Run {args.run_id}", file=sys.stderr)
            return 2
        if run.state != RunState.RUNNING:
            print(f"factory: Run {run.id} is {run.state}, and nothing waits for an answer", file=sys.stderr)
            return 2
        waiting = next((w for w in journal.waiting(run.id) if w.ticket == args.ticket), None)
        command = submit_decide(journal, run.id, args.ticket, args.outcome, args.reason, "cli", waiting)
        while (row := journal.command(command)).state == CommandState.PENDING:
            current = journal.run(run.id)
            if current is None or current.state != RunState.RUNNING:
                print(f"factory: Run {run.id} ended before it took the answer", file=sys.stderr)
                return 2
            machine.clock.sleep(DECIDE_POLL_SECONDS)
        if row.state == CommandState.REJECTED:
            print(f"factory: answer rejected: {row.result}", file=sys.stderr)
            return 1
        print(row.result)
    finally:
        journal.close()
    return 0


def _attach(args: argparse.Namespace, machine: Machine) -> int:
    """Prints the terminal log of the Ticket's latest Attempt and follows it until the Attempt ends.

    It also stops when the Run is no longer running, because no renderer writes to the log then.
    """
    journal = Journal(machine.journal_path, machine.clock)
    try:
        run = journal.run(args.run_id)
        if run is None:
            print(f"factory: no Run {args.run_id}", file=sys.stderr)
            return 2
        logs = list((machine.home / "runs" / run.id / "tickets" / args.ticket).rglob(TERMINAL_LOG))
        if not logs:
            print(f"factory: Ticket {args.ticket} of Run {run.id} has no Attempt to attach to", file=sys.stderr)
            return 2
        log = max(logs, key=lambda path: path.stat().st_mtime_ns)
        offset = 0
        while True:
            current = journal.run(run.id)
            ended = marker(log, "done").exists() or current is None or current.state != RunState.RUNNING
            with log.open("rb") as stream:
                stream.seek(offset)
                data = stream.read()
            offset += len(data)
            print(data.decode(errors="replace"), end="", flush=True)
            if ended and not data:
                return 0
            machine.clock.sleep(ATTACH_POLL_SECONDS)
    finally:
        journal.close()
