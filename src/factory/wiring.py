"""The composition root: `Machine` and one registry per port, where each kind of adapter is picked by its name."""

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from factory.adapters import claude_code, github, gitlab, local_tracker, sbx, terminal_workbench
from factory.adapters.claude_code import Kit
from factory.adapters.local_sandbox import LOCAL
from factory.config import DEFAULT_EXECUTOR, DEFAULT_SANDBOX, DEFAULT_WORKBENCH, Kinds, Project
from factory.core.clock import Clock, SystemClock
from factory.core.git import HostGit
from factory.journal.journal import Journal
from factory.pipeline.library_lookup import Layer, Library
from factory.ports.executor import Executor
from factory.ports.kind import CheckContext, Choice, Kind, Problems, Tools
from factory.ports.run_ports import Ports
from factory.ports.sandbox import Needs, Sandbox
from factory.ports.tracker import PullRequests, SpecLink, Tracker, TrackerError
from factory.ports.workbench import Workbench
from factory.run.clean import Cleaner
from factory.run.run_entry import Orchestrator, frozen_library

# The `factory` Layer of the Library ships inside the package.
FACTORY_LIBRARY = Path(__file__).with_name("library")

# The kind of PullRequests that keeps the PRs in the folder of a Tracker; no git remote picks it,
# so the project file cannot name it as `code_host`.
FOLDER = "folder"


class Registry[K]:
    """A Registry maps the kind names of one port to their Kinds, in the order they were added."""

    def __init__(self) -> None:
        self._kinds: dict[str, K] = {}

    def register(self, name: str, kind: K) -> None:
        self._kinds[name] = kind

    def names(self) -> tuple[str, ...]:
        return tuple(self._kinds)

    def items(self) -> dict[str, K]:
        return dict(self._kinds)

    def values(self) -> list[K]:
        return list(self._kinds.values())

    def copy(self) -> "Registry[K]":
        copy = Registry[K]()
        copy._kinds = dict(self._kinds)
        return copy

    def __getitem__(self, name: str) -> K:
        return self._kinds[name]


@dataclass(frozen=True)
class Registries:
    """Registries hold the Kinds of every port, which a Machine picks its adapters from."""

    executors: Registry[Kind[Executor]] = field(default_factory=Registry)
    sandboxes: Registry[Kind[Sandbox]] = field(default_factory=Registry)
    workbenches: Registry[Kind[Workbench]] = field(default_factory=Registry)
    trackers: Registry[Kind[Tracker]] = field(default_factory=Registry)
    pull_requests: Registry[Kind[PullRequests]] = field(default_factory=Registry)

    def copy(self) -> "Registries":
        """Returns registries of their own with the same kinds, so a kind registered on the copy leaves these alone."""
        return Registries(
            self.executors.copy(),
            self.sandboxes.copy(),
            self.workbenches.copy(),
            self.trackers.copy(),
            self.pull_requests.copy(),
        )


REGISTRIES = Registries()
REGISTRIES.executors.register(DEFAULT_EXECUTOR, claude_code.KIND)
REGISTRIES.sandboxes.register(DEFAULT_SANDBOX, LOCAL)
REGISTRIES.sandboxes.register("sbx", sbx.KIND)
REGISTRIES.workbenches.register(DEFAULT_WORKBENCH, terminal_workbench.TERMINAL)
REGISTRIES.workbenches.register("orca", terminal_workbench.ORCA)
REGISTRIES.trackers.register("github", github.TRACKER)
REGISTRIES.trackers.register("gitlab", gitlab.TRACKER)
REGISTRIES.trackers.register("local", local_tracker.TRACKER)
REGISTRIES.pull_requests.register("github", github.PULL_REQUESTS)
REGISTRIES.pull_requests.register("gitlab", gitlab.PULL_REQUESTS)
REGISTRIES.pull_requests.register(FOLDER, local_tracker.PULL_REQUESTS)


def made[A](kind: Kind[A], tools: Tools) -> A:
    """Makes the adapter of a kind with the settings of a block that names only the kind."""
    return kind.make(kind.parse({}, lambda key, message: None), tools)


def default_home() -> Path:
    return Path(os.environ.get("FACTORY_HOME") or Path.home() / ".local" / "share" / "software-factory")


@dataclass
class Machine:
    """Machine holds the per-machine pieces every command runs with, and tests replace it as a whole.

    `registries` hold the Kinds the project file picks its adapters from, which tests copy to register their fakes,
    `gh` is the command that reaches GitHub, `glab` is the one that reaches GitLab,
    and `stdin` is where the Run's terminal takes the owner's answers when it is a TTY.
    `audit` looks at the Orchestrator of a Run that ended, while its journal is still open.
    `platform` picks the helper that keeps the computer from sleeping while a Run goes.
    The factory config in `config.yaml` of `home` caps the Attempts that all Runs on the machine have going at once.
    """

    home: Path = field(default_factory=default_home)
    clock: Clock = field(default_factory=SystemClock)
    registries: Registries = REGISTRIES
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
        """Returns the Kinds the project file may name, from the registries, with the Checks of every Executor."""
        executors = self.registries.executors
        return Kinds(
            executors=executors.items(),
            sandboxes=self.registries.sandboxes.items(),
            workbenches=self.registries.workbenches.items(),
            code_hosts={name: kind for name, kind in self.registries.pull_requests.items().items() if name != FOLDER},
            checks={name: made(kind, self) for name, kind in executors.items().items()},
        )

    def executor_of(self, project: Project) -> Executor:
        """Makes the Executor the project file's `executor` asks for."""
        return self._make(self.registries.executors, project.executor)

    def sandbox_of(self, project: Project, executor: Executor | None = None) -> Sandbox:
        """Makes the Sandbox of the project and tells it what the Executor and the project file need from it.

        Raises SandboxError when the two clash.
        """
        sandbox = self._make(self.registries.sandboxes, project.sandbox)
        executor = executor or self.executor_of(project)
        sandbox.require(executor.needs().merge(Needs(env=project.sandbox_env)))
        return sandbox

    def workbench_of(self, project: Project) -> Workbench:
        """Makes the Workbench the project file's `workbench` asks for."""
        return self._make(self.registries.workbenches, project.workbench)

    def _make[A](self, registry: Registry[Kind[A]], choice: Choice) -> A:
        return registry[choice.kind].make(choice.settings, self)

    def ports_of(self, project: Project) -> Ports:
        """Builds the ports of a Run from the project file, after the Sandbox checks itself."""
        executor = self.executor_of(project)
        sandbox = self.sandbox_of(project, executor)
        sandbox.check()
        return Ports(executor=executor, sandbox=sandbox, workbench=self.workbench_of(project), clock=self.clock)

    def cleaner_of(self, journal: Journal, run_id: str, project: Project) -> Cleaner:
        """Builds the Cleaner of the leftovers of a Run that is no longer running, with the ports its project names."""
        return Cleaner(
            journal,
            self.home,
            run_id,
            project,
            self._make(self.registries.sandboxes, project.sandbox),
            self.workbench_of(project),
        )

    def problems(self, project: Project, link: SpecLink | None = None) -> Problems:
        """Returns what the kinds the project file picks find in their settings, and with a `link` what the kind
        of its Tracker finds, for `factory check` and the start of a Run."""
        context = CheckContext(project.repo, HostGit(project.repo).remotes(), project.base, self)
        picked = [
            (self.registries.executors[project.executor.kind], project.executor.settings),
            (self.registries.sandboxes[project.sandbox.kind], project.sandbox.settings),
            (self.registries.workbenches[project.workbench.kind], project.workbench.settings),
        ]
        if link is not None:
            picked.append((self.tracker_kind(link.spec), link))
        found = [kind.check(settings, context) for kind, settings in picked]
        return Problems(
            tuple(error for problems in found for error in problems.errors),
            tuple(warning for problems in found for warning in problems.warnings),
        )

    def tracker_kind(self, spec: str) -> Kind[Tracker]:
        """Picks the kind of Tracker from the Spec's link, each kind by its own rule, else the kind without one."""
        kinds = self.registries.trackers.values()
        owner = next((kind for kind in kinds if kind.picks is not None and kind.picks(spec)), None)
        return owner or next(kind for kind in kinds if kind.picks is None)

    def tracker_of(self, link: SpecLink) -> Tracker:
        """Makes the Tracker of the Spec."""
        return self.tracker_kind(link.spec).make(link, self)

    def _served(self, url: str, code_host: Choice | None) -> PullRequests | None:
        """Makes the PullRequests that serve a git remote URL, of the first kind whose rule accepts the URL or else of
        the kind the project file names, or returns None when there is none."""
        kinds = self.registries.pull_requests
        names = [name for name, kind in kinds.items().items() if kind.picks is not None and kind.picks(url)]
        for name in names or ([code_host.kind] if code_host else []):
            try:
                return kinds[name].make(url, self)
            except TrackerError:
                continue
        return None

    def pull_requests_of(self, project: Project, tracker: Tracker) -> PullRequests:
        """Picks where the Run lands from the git remotes of the Target project, apart from the Tracker.

        A remote that points at the repository of the Spec wins,
        else `origin` or the first remote by name that a kind serves.
        Without such a remote a Spec on a code host lands in its own repository, so the landing reports the remote
        that is missing, and any other Spec keeps its PR in the Tracker's folder, through the kind `folder`.
        """
        home = tracker.code_url()
        remotes = sorted(HostGit(project.repo).remotes().items(), key=lambda remote: (remote[0] != "origin", remote[0]))
        served = [pr for _, url in remotes if (pr := self._served(url, project.code_host))]
        if chosen := next((pr for pr in served if home and pr.accepts(home)), served[0] if served else None):
            return chosen
        if home and (fallback := self._served(home, project.code_host)):
            return fallback
        folder = tracker.pull_request_folder
        if folder is None:
            raise TrackerError(
                "no git remote of the project is served by a kind of PullRequests, and the Spec has none;"
                " name the code host with `code_host` in the project file"
            )
        return self.registries.pull_requests[FOLDER].make(str(folder), self)

    def unserved_remotes(self, project: Project) -> list[str]:
        """Returns the git remotes of the Target project, as URLs, when the project has some and no kind serves one."""
        urls = list(HostGit(project.repo).remotes().values())
        if urls and not any(self._served(url, project.code_host) for url in urls):
            return urls
        return []

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
        frozen = frozen_library(self.home, run_id)
        if not frozen.is_dir():
            return self.library
        return lambda repo: Library(
            [Layer("run", frozen)], kit_skills=frozenset(Kit().skills), resolved=self.home / "resolved"
        )

    @property
    def journal_path(self) -> Path:
        return self.home / "factory.db"
