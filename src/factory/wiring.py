"""The composition root: `Machine` and one registry per port, where each kind of adapter is picked by its name."""

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO, cast

from factory import gitlab, sbx
from factory.artifacts import RunDir
from factory.claude_code import ClaudeCodeExecutor, Kit
from factory.clock import Clock, SystemClock
from factory.config import DEFAULT_EXECUTOR, Kinds
from factory.executor import Executor
from factory.github import GitHubTracker, is_issue_link
from factory.gitlab import GitLabTracker
from factory.library_lookup import Layer, Library
from factory.orchestrator import Orchestrator
from factory.sandbox import LOCAL, Sandbox, SandboxChoice, SandboxKind
from factory.tracker import LocalTracker, Tracker, TrackerError
from factory.workbench import OrcaWorkbench, TerminalWorkbench, Workbench

# The `factory` Layer of the Library ships inside the package.
FACTORY_LIBRARY = Path(__file__).with_name("library")


class Registry[F]:
    """A Registry maps the kind names of one port to the factories of their adapters, in the order they were added."""

    def __init__(self) -> None:
        self._factories: dict[str, F] = {}

    def register(self, kind: str, factory: F) -> None:
        self._factories[kind] = factory

    def names(self) -> tuple[str, ...]:
        return tuple(self._factories)

    def items(self) -> dict[str, F]:
        return dict(self._factories)

    def values(self) -> list[F]:
        return list(self._factories.values())

    def __getitem__(self, kind: str) -> F:
        return self._factories[kind]


@dataclass(frozen=True)
class TrackerKind:
    """A TrackerKind owns the Specs whose link `owns` accepts and makes their Tracker from the Spec, the Ticket ids
    and the Machine. A kind without `owns` takes every Spec the others do not own.
    `preflight` returns what keeps the Tracker's CLI from serving a Run, given the git remotes of the Target project
    and its path, and `warnings` returns what the owner should know about the Run's `base`.
    """

    make: Callable[[str, list[int] | None, "Machine"], Tracker]
    owns: Callable[[str], bool] | None = None
    preflight: Callable[[Tracker, dict[str, str], Path], list[str]] | None = None
    warnings: Callable[[Tracker, str], list[str]] | None = None


def _gitlab_warnings(tracker: Tracker, base: str) -> list[str]:
    """Warns that GitLab closes issues only on a merge into the default branch, which `base` may not be."""
    gitlab_tracker = cast(GitLabTracker, tracker)
    try:
        default = gitlab_tracker.default_branch()
    except TrackerError as error:
        return [f"cannot tell the default branch of {gitlab_tracker.project}: {error}"]
    if default == base:
        return []
    return [
        f"base {base} is not the default branch {default} of {gitlab_tracker.project},"
        " so GitLab will not close the merged Tickets and they stay open"
    ]


EXECUTORS = Registry[Callable[[], Executor]]()
SANDBOXES = Registry[SandboxKind]()
WORKBENCHES = Registry[Callable[[], Workbench]]()
TRACKERS = Registry[TrackerKind]()

EXECUTORS.register(DEFAULT_EXECUTOR, ClaudeCodeExecutor)
SANDBOXES.register("local", LOCAL)
SANDBOXES.register("sbx", sbx.KIND)
WORKBENCHES.register("terminal", TerminalWorkbench)
WORKBENCHES.register("orca", OrcaWorkbench)
TRACKERS.register(
    "github",
    TrackerKind(lambda spec, tickets, machine: GitHubTracker(spec, tickets, command=machine.gh), is_issue_link),
)
TRACKERS.register(
    "gitlab",
    TrackerKind(
        lambda spec, tickets, machine: GitLabTracker(spec, tickets, command=machine.glab),
        gitlab.is_issue_link,
        lambda tracker, remotes, repo: cast(GitLabTracker, tracker).preflight(remotes, repo),
        _gitlab_warnings,
    ),
)
TRACKERS.register("local", TrackerKind(lambda spec, tickets, machine: LocalTracker(Path(spec))))


def make_sandbox(choice: SandboxChoice) -> Sandbox:
    return SANDBOXES[choice.kind].make(choice.settings)


def make_workbench(kind: str) -> Workbench:
    return WORKBENCHES[kind]()


def kinds() -> Kinds:
    """Returns the kind names the project file may use, from the registries."""
    return Kinds(executors=EXECUTORS.names(), sandboxes=SANDBOXES.items(), workbenches=WORKBENCHES.names())


def _owner(spec: str) -> TrackerKind | None:
    """Returns the Tracker kind that owns the Spec by its link, or None for the folder of a LocalTracker."""
    return next((kind for kind in TRACKERS.values() if kind.owns is not None and kind.owns(spec)), None)


def tracker_owned(spec: str) -> bool:
    """Tells whether a Tracker kind owns the Spec by its link, as opposed to the folder of a LocalTracker."""
    return _owner(spec) is not None


def default_home() -> Path:
    return Path(os.environ.get("FACTORY_HOME") or Path.home() / ".local" / "share" / "software-factory")


@dataclass
class Machine:
    """Machine holds the per-machine pieces every command runs with, and tests replace it as a whole.

    `executor` replaces the one the project file's `executor` asks for,
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
    executor: Executor | None = None
    sandbox_for: Callable[[SandboxChoice], Sandbox] = make_sandbox
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

    @property
    def kinds(self) -> Kinds:
        return kinds()

    def executor_for(self, kind: str) -> Executor:
        """Returns the Executor of this Machine, or else the one the project file names."""
        return self.executor or EXECUTORS[kind]()

    def workbench_for(self, kind: str) -> Workbench:
        """Returns the Workbench of this Machine, or else the one the project file names."""
        return self.workbench or make_workbench(kind)

    def tracker_kind(self, spec: str) -> TrackerKind:
        """Picks the kind of Tracker from the Spec's link, each kind by its own rule, else a folder."""
        return _owner(spec) or next(kind for kind in TRACKERS.values() if kind.owns is None)

    def tracker_of(self, spec: str, tickets: list[int] | None) -> Tracker:
        """Makes the Tracker of the Spec."""
        return self.tracker_kind(spec).make(spec, tickets, self)

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
