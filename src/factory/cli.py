"""CLI `factory`: the only interface that starts and inspects Runs."""

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from factory.clock import Clock, SystemClock
from factory.config import ConfigError, load_project
from factory.executor import Executor
from factory.git import GitError
from factory.journal import Journal
from factory.orchestrator import Orchestrator, Ports, integration_branch, new_run_id
from factory.run_view import RunView
from factory.sandbox import LocalSandbox, Sandbox
from factory.tracker import LocalTracker, TrackerError
from factory.workbench import TerminalWorkbench, Workbench


def default_home() -> Path:
    return Path(os.environ.get("FACTORY_HOME") or Path.home() / ".local" / "share" / "software-factory")


@dataclass
class Context:
    """The machine-level pieces a command runs with, replaced as a whole by tests."""

    home: Path = field(default_factory=default_home)
    clock: Clock = field(default_factory=SystemClock)
    executor: Executor | None = None
    sandbox: Sandbox = field(default_factory=LocalSandbox)
    workbench: Workbench = field(default_factory=TerminalWorkbench)

    @property
    def journal_path(self) -> Path:
        return self.home / "factory.db"


def main(argv: list[str] | None = None, context: Context | None = None) -> int:
    args = _parser().parse_args(argv)
    context = context or Context()
    try:
        code: int = args.command(args, context)
    except (ConfigError, TrackerError, GitError) as error:
        print(f"factory: {error}", file=sys.stderr)
        return 2
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory")
    commands = parser.add_subparsers(required=True)

    run = commands.add_parser("run", help="run a Spec through the Pipeline")
    run.add_argument("spec", type=Path, help="Spec folder of a LocalTracker")
    run.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    run.set_defaults(command=_run)

    status = commands.add_parser("status", help="show a Run from the journal")
    status.add_argument("run_id")
    status.set_defaults(command=_status)
    return parser


def _run(args: argparse.Namespace, context: Context) -> int:
    project = load_project(args.project)
    if context.executor is None:
        print("factory: no Executor is available in this build", file=sys.stderr)
        return 2
    ports = Ports(executor=context.executor, sandbox=context.sandbox, workbench=context.workbench, clock=context.clock)
    journal = Journal(context.journal_path, context.clock)
    try:
        run_id = new_run_id(context.clock)
        print(f"Run {run_id} started on {integration_branch(run_id)}", flush=True)
        orchestrator = Orchestrator(journal, project, ports, context.home, RunView(sys.stdout))
        orchestrator.start(run_id, LocalTracker(args.spec))
        state = orchestrator.run()
    finally:
        journal.close()
    print(f"Run {run_id} finished: {state}")
    return 0 if state == "done" else 1


def _status(args: argparse.Namespace, context: Context) -> int:
    journal = Journal(context.journal_path, context.clock)
    try:
        run = journal.run(args.run_id)
        if run is None:
            print(f"factory: no Run {args.run_id}", file=sys.stderr)
            return 2
        print(f"Run {run.id}  {run.state}  {run.integration_branch}")
        rows = [("TICKET", "NODE", "STAGE", "ROUND", "ATTEMPT", "STATE")]
        for t in journal.tickets(run.id):
            rows.append((t.id, t.node or "-", t.stage or "-", str(t.round), str(t.attempt), t.state))
        widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
        for row in rows:
            print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
    finally:
        journal.close()
    return 0
