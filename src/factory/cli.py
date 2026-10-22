"""The `factory` CLI is the only interface that starts and inspects Runs."""

import argparse
import os
import sys
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path

from factory.claude_code import ClaudeCodeExecutor
from factory.clock import Clock, SystemClock
from factory.config import ConfigError, display_path, load_project
from factory.executor import Executor
from factory.git import GitError
from factory.journal import Journal
from factory.orchestrator import Orchestrator, Ports, integration_branch, new_run_id
from factory.run_view import RunView
from factory.sandbox import LocalSandbox, Sandbox
from factory.states import RunState
from factory.tracker import LocalTracker, TrackerError
from factory.workbench import TerminalWorkbench, Workbench, WorkbenchError


def default_home() -> Path:
    return Path(os.environ.get("FACTORY_HOME") or Path.home() / ".local" / "share" / "software-factory")


@dataclass
class Machine:
    """Machine holds the per-machine pieces every command runs with, and tests replace it as a whole."""

    home: Path = field(default_factory=default_home)
    clock: Clock = field(default_factory=SystemClock)
    executor: Executor = field(default_factory=ClaudeCodeExecutor)
    sandbox: Sandbox = field(default_factory=LocalSandbox)
    workbench: Workbench = field(default_factory=TerminalWorkbench)

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
    except (ConfigError, TrackerError, GitError, WorkbenchError) as error:
        for message in error.errors if isinstance(error, ConfigError) else [str(error)]:
            print(f"factory: {message}", file=sys.stderr)
        return 2
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory")
    parser.add_argument("--version", action="version", version=f"factory {version('software-factory')}")
    commands = parser.add_subparsers(required=True)

    run = commands.add_parser("run", help="run a Spec through the Pipeline")
    run.add_argument("spec", type=Path, help="Spec folder of a LocalTracker")
    run.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    run.set_defaults(command=_run)

    check = commands.add_parser("check", help="check the project file and its Pipeline before a Run")
    check.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    check.set_defaults(command=_check)

    status = commands.add_parser("status", help="show a Run from the journal")
    status.add_argument("run_id")
    status.set_defaults(command=_status)
    return parser


def _run(args: argparse.Namespace, machine: Machine) -> int:
    project = load_project(args.project)
    ports = Ports(executor=machine.executor, sandbox=machine.sandbox, workbench=machine.workbench, clock=machine.clock)
    journal = Journal(machine.journal_path, machine.clock)
    run_id = new_run_id(machine.clock)
    print(f"Run {run_id} started on {integration_branch(run_id)}", flush=True)
    orchestrator = Orchestrator(journal, project, ports, machine.home, RunView(sys.stdout))
    try:
        orchestrator.start(run_id, LocalTracker(args.spec))
        state = orchestrator.run()
    except Exception as error:
        orchestrator.abort(str(error))
        raise
    finally:
        journal.close()
    print(f"Run {run_id} finished: {state}")
    return 0 if state == RunState.DONE else 1


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
        print(f"Run {run.id}  {run.state}  {run.integration_branch}")
        rows = [("TICKET", "NODE", "LAST STAGE", "ROUND", "ATTEMPT", "STATE")]
        for t in journal.tickets(run.id):
            rows.append((t.id, t.node or "-", t.last_stage or "-", str(t.round), str(t.attempt), t.state))
        widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
        for row in rows:
            print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
    finally:
        journal.close()
    return 0
