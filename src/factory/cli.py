"""The `factory` CLI is the only interface that starts and inspects Runs."""

import argparse
import json
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

from factory import gitlab
from factory.artifacts import RunDir
from factory.attempt import Ports
from factory.claude_code import ClaudeCodeExecutor, Kit
from factory.clean import Cleaner
from factory.clock import Clock, SystemClock
from factory.commands import Cancel, Command, Limits, decide_for, send
from factory.commands import Override as OverrideCommand
from factory.config import ConfigError, Project, display_path, load_project, load_slots
from factory.custody import RunCustody, RunTakenOver
from factory.events import Event
from factory.executor import Executor
from factory.fingerprint import roles
from factory.git import GitError, HostGit
from factory.github import GitHubTracker, is_issue_link
from factory.gitlab import GitLabTracker
from factory.journal import EventRow, Journal, RunRow
from factory.library_lookup import Layer, Library, LibraryError
from factory.metrics import run_metrics
from factory.orchestrator import Orchestrator, integration_branch, new_run_id, resolve_base
from factory.render import marker
from factory.run_view import (
    RunView,
    ci_wait,
    format_duration,
    format_spend,
    pause_note,
    run_phase,
    run_seconds,
    ticket_seconds,
    waits,
)
from factory.sandbox import LocalSandbox, Sandbox, SandboxError, SandboxSettings
from factory.sbx import SbxSandbox
from factory.sleep_block import SleepBlock
from factory.states import OOM, OOM_ADVICE, RunState, TicketState
from factory.stats import GROUPS, render, report
from factory.throttle import SHARES
from factory.timeline import timeline
from factory.tracker import LocalTracker, Tracker, TrackerError
from factory.workbench import TERMINAL_LOG, OrcaWorkbench, TerminalWorkbench, Workbench, WorkbenchError

# A failure of one of these is a host tool the owner can fix, such as `git`, `gh`, `sbx` or `orca`.
HOST_TOOL_ERRORS = (GitError, WorkbenchError, SandboxError, TrackerError)

# `attach` looks this often, in seconds, for new lines in the terminal log of the Attempt.
ATTACH_POLL_SECONDS = 0.1

# The `factory` Layer of the Library ships inside the package.
FACTORY_LIBRARY = Path(__file__).with_name("library")


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
    that reaches GitHub, `glab` is the one that reaches GitLab,
    and `stdin` is where the Run's terminal takes the owner's answers when it is a TTY.
    `audit` looks at the Orchestrator of a Run that ended, while its journal is still open.
    `platform` picks the helper that keeps the computer from sleeping while a Run goes.
    The factory config in `config.yaml` of `home` caps the Attempts that all Runs on the machine have going at once.
    """

    home: Path = field(default_factory=default_home)
    clock: Clock = field(default_factory=SystemClock)
    executor: Executor = field(default_factory=ClaudeCodeExecutor)
    sandbox_for: Callable[[SandboxSettings], Sandbox] = make_sandbox
    workbench: Workbench | None = None
    gh: Sequence[str] = ("gh",)
    glab: Sequence[str] = ("glab",)
    stdin: TextIO = field(default_factory=lambda: sys.stdin)
    audit: Callable[[Orchestrator], None] | None = None
    platform: str = sys.platform
    factory_library: Path = FACTORY_LIBRARY

    @property
    def config_path(self) -> Path:
        return self.home / "config.yaml"

    def library(self, repo: Path) -> Library:
        """Returns the Library of the Layers `project`, `user` and `factory` for the Target project at `repo`."""
        return Library(
            [
                Layer("project", repo / ".factory"),
                Layer("user", self.home / "library"),
                Layer("factory", self.factory_library),
            ],
            kit_skills=frozenset(Kit().skills),
            resolved=self.home / "resolved",
        )

    def run_library(self, run_id: str) -> Callable[[Path], Library]:
        """Returns the Library lookup of a Run, which is the Run's own Layer once it has resolved the Library."""
        frozen = RunDir(self.home / "runs" / run_id).library
        if not frozen.is_dir():
            return self.library
        return lambda repo: Library(
            [Layer("run", frozen)], kit_skills=frozenset(Kit().skills), resolved=self.home / "resolved"
        )

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
    except (ConfigError, *HOST_TOOL_ERRORS, RunTakenOver) as error:
        for message in error.errors if isinstance(error, ConfigError) else [str(error)]:
            print(f"factory: {message}", file=sys.stderr)
        return 2
    return code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory")
    parser.add_argument("--version", action="version", version=f"factory {version('software-factory')}")
    commands = parser.add_subparsers(required=True)

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
    run.set_defaults(command=_run)

    check = commands.add_parser("check", help="check the project file and its Pipeline before a Run")
    check.add_argument(
        "spec", nargs="?", help="link to the GitLab issue of the Spec, whose `glab` access and git remote to check"
    )
    check.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    check.add_argument("--show", metavar="ROLE", help="print the final prompt and schema of a Role of the Library")
    check.set_defaults(command=_check)

    status = commands.add_parser("status", help="show a Run from the journal")
    status.add_argument("run_id")
    status.add_argument("--timeline", action="store_true", help="list the nodes the Run entered, in order")
    status.set_defaults(command=_status)

    stats = commands.add_parser("stats", help="compare Runs and variants on their metrics")
    stats.add_argument("run_ids", nargs="+", metavar="run")
    stats.add_argument("--by", choices=sorted(GROUPS), help="group Attempts and Tickets by a fingerprint field")
    stats.add_argument("--stage", help="show only this Stage")
    stats.add_argument("--json", action="store_true", help="print the data as JSON")
    stats.set_defaults(command=_stats)

    decide = commands.add_parser("decide", help="answer the Decision of a Ticket that waits for you")
    decide.add_argument("run_id")
    decide.add_argument("ticket")
    decide.add_argument("outcome")
    decide.add_argument("--reason", action="append", default=[], help="why; required to send the Ticket back to work")
    decide.set_defaults(command=_decide)

    cancel = commands.add_parser(
        "cancel", help="abandon a Ticket, which keeps the Tickets that depend on it from starting"
    )
    cancel.add_argument("run_id")
    cancel.add_argument("ticket")
    cancel.set_defaults(command=_cancel)

    override = commands.add_parser("override", help="replace the outcome of the current or last Decision of a Ticket")
    override.add_argument("run_id")
    override.add_argument("ticket")
    override.add_argument("outcome")
    override.add_argument("--reason", action="append", default=[], help="why; required to send the Ticket back to work")
    override.set_defaults(command=_override)

    limits = commands.add_parser("limits", help="change the parallelism and the round limits of a Run")
    limits.add_argument("run_id")
    limits.add_argument("--max-parallel", type=_positive, help="Tickets at work at once")
    limits.add_argument(
        "--rounds", action="append", default=[], type=_stage_limit, metavar="STAGE=N", help="round limit of a Stage"
    )
    for share in SHARES:
        limits.add_argument(
            f"--{share}",
            action="append",
            default=[],
            type=_window_share,
            metavar="WINDOW=SHARE",
            help=f"{share} of a subscription window such as five_hour=0.5",
        )
    limits.add_argument(
        "--no-throttle", action="append", default=[], metavar="WINDOW", help="lift the brake on a subscription window"
    )
    limits.set_defaults(command=_limits)

    resume = commands.add_parser("resume", help="continue an orphaned Run from the journal")
    resume.add_argument("run_id")
    resume.add_argument(
        "--no-prevent-sleep", action="store_true", help="let the computer sleep when idle while the Run goes"
    )
    resume.set_defaults(command=_resume)

    finish = commands.add_parser("finish", help="end an orphaned Run with the Tickets it merged and open its PR")
    finish.add_argument("run_id")
    finish.set_defaults(command=_finish)

    clean = commands.add_parser(
        "clean", help="list the Workspaces, Sandboxes and branches that Runs no longer running left, and remove them"
    )
    clean.add_argument("--yes", action="store_true", help="remove them; without it only print what would go")
    clean.set_defaults(command=_clean)

    attach = commands.add_parser("attach", help="follow the terminal of the latest Attempt of a Ticket")
    attach.add_argument("run_id")
    attach.add_argument("ticket")
    attach.set_defaults(command=_attach)
    return parser


def _positive(text: str) -> int:
    try:
        number = int(text)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _stage_limit(text: str) -> tuple[str, int]:
    stage, _, limit = text.partition("=")
    if not stage:
        raise argparse.ArgumentTypeError("must be STAGE=N such as fix=3")
    return stage, _positive(limit)


def _window_share(text: str) -> tuple[str, float]:
    window, _, share = text.partition("=")
    try:
        return window, float(share)
    except ValueError:
        raise argparse.ArgumentTypeError("must be WINDOW=SHARE such as five_hour=0.5") from None


def _issue_numbers(text: str) -> list[int]:
    try:
        return [int(item) for item in text.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("must be issue numbers such as 28,30") from None


def _tracker(args: argparse.Namespace, machine: Machine) -> Tracker:
    if args.tickets is not None and not _is_link(args.spec):
        raise TrackerError("--tickets takes sub-issues of a GitHub Spec, not Tickets of a folder")
    return _tracker_of(args.spec, args.tickets, machine)


def _is_link(spec: str) -> bool:
    return is_issue_link(spec) or gitlab.is_issue_link(spec)


def _tracker_of(spec: str, tickets: list[int] | None, machine: Machine) -> Tracker:
    """Picks the Tracker from the Spec's link: GitHub by its host, GitLab by its `/-/issues/` path, else a folder."""
    if is_issue_link(spec):
        return GitHubTracker(spec, tickets, command=machine.gh)
    if gitlab.is_issue_link(spec):
        return GitLabTracker(spec, tickets, command=machine.glab)
    return LocalTracker(Path(spec))


def _preflight(tracker: Tracker, project: Project) -> None:
    """Refuses to go on when the Tracker's CLI cannot serve a Run, and names the fix of each problem."""
    if isinstance(tracker, GitLabTracker):
        problems = tracker.preflight(HostGit(project.repo).remotes(), project.repo)
        if problems:
            raise ConfigError(*problems)


def _run(args: argparse.Namespace, machine: Machine) -> int:
    project = load_project(args.project, machine.library)
    tracker = _tracker(args, machine)
    _preflight(tracker, project)
    sandbox = machine.sandbox_for(project.sandbox)
    sandbox.check()
    slots = load_slots(machine.config_path)
    snapshot = tracker.read_spec()
    if isinstance(tracker, GitLabTracker):
        _warn_unless_default(tracker, project.base)
    git = HostGit(project.repo)
    base_commit = resolve_base(git, project.base, tracker.pull_requests)
    if base_commit is None:
        print(f"factory: the Run pushes to no git remote, so it starts from the local {project.base}", file=sys.stderr)
        base_commit = git.resolve(project.base)
    ports = Ports(
        executor=machine.executor,
        sandbox=sandbox,
        workbench=machine.workbench or make_workbench(project.workbench),
        clock=machine.clock,
    )
    journal = Journal(machine.journal_path, machine.clock)
    run_id = new_run_id(machine.clock)
    print(f"Run {run_id} started on {integration_branch(run_id)}", flush=True)
    assert project.library is not None
    frozen = RunDir(machine.home / "runs" / run_id).library
    project.library.freeze(roles(project.pipeline), project.pipeline.path, frozen)
    orchestrator = Orchestrator(
        journal, project, ports, machine.home, RunView(sys.stdout, machine.clock, machine.stdin), slots
    )
    return _drive(
        orchestrator,
        journal,
        lambda: orchestrator.start(run_id, snapshot, tracker, base_commit),
        machine,
        args.no_prevent_sleep,
    )


def _warn_unless_default(tracker: GitLabTracker, base: str) -> None:
    """Warns that GitLab closes issues only on a merge into the default branch, which `base` may not be."""
    try:
        default = tracker.default_branch()
    except TrackerError as error:
        print(f"factory: warning: cannot tell the default branch of {tracker.project}: {error}", file=sys.stderr)
        return
    if default != base:
        print(
            f"factory: warning: base {base} is not the default branch {default} of {tracker.project},"
            " so GitLab will not close the merged Tickets and they stay open",
            file=sys.stderr,
        )


def _drive(
    orchestrator: Orchestrator, journal: Journal, begin: Callable[[], None], machine: Machine, no_prevent_sleep: bool
) -> int:
    """Begins the Run, ticks it to its end and prints how it ended, and returns the exit code.

    Unless the project file or `no_prevent_sleep` turns it off, the computer stays awake while the Run goes,
    and the block is released when it ends.
    """
    block = SleepBlock([])
    try:
        with _sigterm_as_exit():
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
            took = _took(journal, orchestrator.run_id, machine.clock)
    except (KeyboardInterrupt, SystemExit) as error:
        # Ctrl-C and `kill` leave the Run orphaned, so its slots go back to the machine and `finish` can end it.
        orchestrator.interrupt(str(error) or type(error).__name__)
        raise
    except HOST_TOOL_ERRORS as error:
        # A host tool the owner can fix leaves the Run orphaned, so `resume` goes on once the host works again.
        if not orchestrator.started:
            raise
        orchestrator.interrupt(str(error))
        _tell_orphaned(orchestrator.run_id, error)
        return 2
    except BaseException as error:
        orchestrator.abort(str(error) or type(error).__name__)
        raise
    finally:
        block.release()
        journal.close()
    _report(orchestrator, state, took)
    return 0 if state == RunState.DONE else 1


def _tell_orphaned(run_id: str, error: Exception) -> None:
    """Names the host tool call that failed and the commands that continue or end the orphaned Run."""
    print(f"factory: {error}", file=sys.stderr)
    print(f"Run {run_id} is orphaned. Fix the host, then continue the Run or end it:", file=sys.stderr)
    print(f"factory resume {run_id}", file=sys.stderr)
    print(f"factory finish {run_id}", file=sys.stderr)


def _took(journal: Journal, run_id: str, clock: Clock) -> str:
    """Returns how long the Run went and what its Attempts cost, such as `41m 12s, $3.20`.

    A Run that ends without `RunFinished`, such as a paused one, has gone until now.
    """
    run = journal.run(run_id)
    assert run is not None
    return f"{format_duration(run_seconds(run, clock.now()))}, {format_spend(run.spend)}"


def _report(orchestrator: Orchestrator, state: RunState, took: str) -> None:
    """Prints how the Run ended, its pull request and the commands that land it by hand."""
    ci = f", {orchestrator.ci_note}" if orchestrator.ci_note else ""
    print(f"Run {orchestrator.run_id} finished: {state} in {took}{ci}")
    if orchestrator.pull_request:
        print(f"Pull request: {orchestrator.pull_request}")
    if orchestrator.by_hand:
        print("Land it by hand:")
        for command in orchestrator.by_hand:
            print(f"  {shlex.join(command)}")


def _resume(args: argparse.Namespace, machine: Machine) -> int:
    """Continues an orphaned Run, or a running one whose heartbeat is stale, from its events in the journal."""
    taken = _take_orphaned(args.run_id, machine, "resumes")
    if isinstance(taken, int):
        return taken
    journal, run, orchestrator, tracker = taken
    print(f"Run {run.id} resumed on {run.integration_branch}", flush=True)
    return _drive(
        orchestrator,
        journal,
        lambda: orchestrator.resume(run.id, tracker),
        machine,
        args.no_prevent_sleep,
    )


def _finish(args: argparse.Namespace, machine: Machine) -> int:
    """Ends an orphaned Run as `partial` with the Tickets it merged, and lands the Integration branch if any merged."""
    taken = _take_orphaned(args.run_id, machine, "finishes")
    if isinstance(taken, int):
        return taken
    journal, run, orchestrator, tracker = taken
    merged = any(t.state == TicketState.MERGED for t in journal.tickets(run.id))
    print(f"Run {run.id} finishing on {run.integration_branch}", flush=True)
    try:
        with _sigterm_as_exit():
            state = orchestrator.finish(run.id, tracker)
            took = _took(journal, run.id, machine.clock)
    except (KeyboardInterrupt, SystemExit) as error:
        # A `finish` stopped halfway leaves the Run orphaned again, so `finish` can be run once more.
        orchestrator.interrupt(str(error) or type(error).__name__)
        raise
    finally:
        journal.close()
    _report(orchestrator, state, took)
    if not merged:
        print(f"No Ticket of Run {run.id} merged, so no pull request was opened")
    return 0


def _take_orphaned(run_id: str, machine: Machine, verb: str) -> tuple[Journal, RunRow, Orchestrator, Tracker] | int:
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
        project = load_project(Path(started["project"]), machine.run_library(run.id))
        spec = run.spec_ref
        tracker = _tracker_of(spec, None, machine)
        sandbox = machine.sandbox_for(project.sandbox)
        sandbox.check()
        ports = Ports(
            executor=machine.executor,
            sandbox=sandbox,
            workbench=machine.workbench or make_workbench(project.workbench),
            clock=machine.clock,
        )
        orchestrator = Orchestrator(
            journal,
            project,
            ports,
            machine.home,
            RunView(sys.stdout, machine.clock, machine.stdin),
            load_slots(machine.config_path),
        )
    except BaseException:
        journal.close()
        raise
    return journal, run, orchestrator, tracker


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


def _clean(args: argparse.Namespace, machine: Machine) -> int:
    """Prints the leftovers of the Runs that are no longer running, and with `--yes` removes them.

    A Run is no longer running when it ended or its heartbeat went stale, and a Run that still runs is not touched.
    """
    journal = Journal(machine.journal_path, machine.clock)
    found = False
    try:
        RunCustody(journal, machine.clock).orphan_stale()
        for run_id in journal.run_ids():
            run = journal.run(run_id)
            assert run is not None
            if run.state == RunState.RUNNING:
                continue
            try:
                project = load_project(Path(journal.events(run.id)[0].payload["project"]), machine.run_library(run.id))
            except ConfigError as error:
                print(f"Run {run.id} ({run.state}): skipped, its project file does not load: {'; '.join(error.errors)}")
                continue
            cleaner = Cleaner(
                journal,
                machine.home,
                run.id,
                project,
                machine.sandbox_for(project.sandbox),
                machine.workbench or make_workbench(project.workbench),
            )
            for leftover in cleaner.find():
                if not found:
                    print("Leftovers of Runs that are no longer running:")
                    found = True
                print(f"Run {run.id} ({run.state}), Ticket {leftover.ticket}:")
                for line in cleaner.clean(leftover) if args.yes else cleaner.plan(leftover):
                    print(f"  {line}")
    finally:
        journal.close()
    if not found:
        print("Nothing to clean")
    elif not args.yes:
        print("Nothing was changed; run `factory clean --yes` to remove them")
    return 0


def _check(args: argparse.Namespace, machine: Machine) -> int:
    project = load_project(args.project, machine.library)
    library = machine.library(project.repo)
    if errors := library.check():
        raise ConfigError(*errors)
    if args.show is not None:
        try:
            print(library.show(args.show))
        except LibraryError as error:
            raise ConfigError(str(error)) from error
        return 0
    if args.spec is not None:
        _preflight(_tracker_of(args.spec, None, machine), project)
    print(f"{display_path(project.path)}: OK")
    return 0


def _status(args: argparse.Namespace, machine: Machine) -> int:
    journal = Journal(machine.journal_path, machine.clock)
    try:
        RunCustody(journal, machine.clock).orphan_stale()
        run = journal.run(args.run_id)
        if run is None:
            print(f"factory: no Run {args.run_id}", file=sys.stderr)
            return 2
        if args.timeline:
            print("\n".join(timeline(journal.events(run.id))))
            return 0
        now = machine.clock.now()
        took = format_duration(run_seconds(run, now))
        print(
            f"Run {run.id}  {run.state}  {run.integration_branch}{pause_note(run)}  {took}  {format_spend(run.spend)}"
        )
        tickets = journal.tickets(run.id)
        rows = [("TICKET", "NODE", "LAST STAGE", "ROUND", "ATTEMPT", "LOST", "STATE", "DURATION", "COST")]
        for t in tickets:
            ran = format_duration(ticket_seconds(t, now)) if t.claimed_at else "-"
            rows.append(
                (
                    t.id,
                    t.node or "-",
                    t.last_stage or "-",
                    str(t.round),
                    str(t.attempt),
                    str(t.sandbox_losses),
                    t.state,
                    ran,
                    format_spend(t.spend),
                )
            )
        widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
        for row in rows:
            print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
        events = journal.events(run.id)
        phase = run_phase(run, events)
        if phase != run.state:
            print(f"Run phase: {phase}")
        if (ci := ci_wait(run, events)) is not None:
            print(f"Run {ci}")
        for ticket, wait in waits(tickets, events).items():
            print(f"Ticket {ticket} {wait}")
        ooms = _ooms(events)
        for t in tickets:
            if t.last_loss:
                print(f"Ticket {t.id} lost its Sandbox {t.sandbox_losses}x, last: {t.last_loss}")
            if t.id in ooms:
                print(f"Ticket {t.id} ran out of memory in its Sandbox{ooms[t.id]}: {OOM_ADVICE}")
            if t.kept_ref:
                print(f"Ticket {t.id} abandoned, its work is in {t.kept_ref}")
    finally:
        journal.close()
    return 0


def _stats(args: argparse.Namespace, machine: Machine) -> int:
    journal = Journal(machine.journal_path, machine.clock)
    try:
        missing = [run_id for run_id in args.run_ids if journal.run(run_id) is None]
        if missing:
            print(f"factory: no Run {', '.join(missing)}", file=sys.stderr)
            return 2
        runs = [run_metrics(run_id, journal.events(run_id)) for run_id in args.run_ids]
    finally:
        journal.close()
    data = report(runs, args.by, args.stage)
    print(json.dumps(data, indent=2) if args.json else "\n".join(render(data)))
    return 0


def _ooms(events: list[EventRow]) -> dict[str, str]:
    """Returns the Tickets whose last failed Attempt the OOM killer killed, with the Sandbox's limit and peak."""
    found: dict[str, str] = {}
    for event in events:
        if event.type != Event.AttemptFinished:
            continue
        ticket = event.payload["ticket"]
        if event.payload.get("cause") != OOM:
            found.pop(ticket, None)
            continue
        facts = [f"limit {event.payload['memory_limit']}"] if "memory_limit" in event.payload else []
        if "memory_peak" in event.payload:
            facts.append(f"peak {event.payload['memory_peak']} bytes")
        found[ticket] = f" ({', '.join(facts)})" if facts else ""
    return found


def _decide(args: argparse.Namespace, machine: Machine) -> int:
    journal = Journal(machine.journal_path, machine.clock)
    try:
        run = journal.run(args.run_id)
        waiting = next((w for w in journal.waiting(args.run_id) if w.ticket == args.ticket), None) if run else None
    except BaseException:
        journal.close()
        raise
    return _send(journal, machine, args.run_id, decide_for(args.ticket, args.outcome, args.reason, "cli", waiting))


def _cancel(args: argparse.Namespace, machine: Machine) -> int:
    return _send(Journal(machine.journal_path, machine.clock), machine, args.run_id, Cancel(args.ticket))


def _override(args: argparse.Namespace, machine: Machine) -> int:
    command = OverrideCommand(args.ticket, args.outcome, args.reason)
    return _send(Journal(machine.journal_path, machine.clock), machine, args.run_id, command)


def _limits(args: argparse.Namespace, machine: Machine) -> int:
    throttle: dict[str, dict[str, float] | None] = {window: None for window in args.no_throttle}
    for share in SHARES:
        for window, value in getattr(args, share):
            if throttle.get(window, {}) is None:
                print(f"factory: {window} is both lifted and set", file=sys.stderr)
                return 2
            throttle.setdefault(window, {})[share] = value  # type: ignore[index]
    if args.max_parallel is None and not args.rounds and not throttle:
        print("factory: limits needs --max-parallel, --rounds, --cap, --pace or --no-throttle", file=sys.stderr)
        return 2
    command = Limits(args.max_parallel, dict(args.rounds), throttle)
    return _send(Journal(machine.journal_path, machine.clock), machine, args.run_id, command)


def _send(journal: Journal, machine: Machine, run_id: str, command: Command) -> int:
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
