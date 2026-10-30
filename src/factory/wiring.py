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
from factory.config import DEFAULT_EXECUTOR, Kinds, ModelNames, Project
from factory.executor import Executor
from factory.git import HostGit
from factory.github import GitHubPullRequests, GitHubTracker, is_issue_link
from factory.github import is_remote_url as is_github_remote
from factory.gitlab import GitLabPullRequests, GitLabTracker
from factory.gitlab import is_hosted_remote_url as is_gitlab_remote
from factory.library_lookup import Layer, Library
from factory.orchestrator import Orchestrator
from factory.sandbox import LOCAL, Sandbox, SandboxChoice, SandboxKind
from factory.tracker import LocalTracker, PullRequestFolder, PullRequests, Tracker, TrackerError
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
    `code_url` returns the git URL of the repository the Spec lives in, which a Run lands in when its project has no
    remote that tells otherwise, and a kind without it keeps the PR in the folder `folder` returns.
    """

    make: Callable[[str, list[int] | None, "Machine"], Tracker]
    owns: Callable[[str], bool] | None = None
    preflight: Callable[[Tracker, dict[str, str], Path], list[str]] | None = None
    warnings: Callable[[Tracker, str], list[str]] | None = None
    code_url: Callable[[Tracker], str] | None = None
    folder: Callable[[Tracker], PullRequestFolder] | None = None


@dataclass(frozen=True)
class PullRequestsKind:
    """A PullRequestsKind lands a Run on the code host whose git remote URLs `serves` accepts, and `make` makes its
    PullRequests from such a URL and the Machine.
    """

    serves: Callable[[str], bool]
    make: Callable[[str, "Machine"], PullRequests]


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
PULL_REQUESTS = Registry[PullRequestsKind]()

EXECUTORS.register(DEFAULT_EXECUTOR, ClaudeCodeExecutor)
SANDBOXES.register("local", LOCAL)
SANDBOXES.register("sbx", sbx.KIND)
WORKBENCHES.register("terminal", TerminalWorkbench)
WORKBENCHES.register("orca", OrcaWorkbench)
TRACKERS.register(
    "github",
    TrackerKind(
        lambda spec, tickets, machine: GitHubTracker(spec, tickets, command=machine.gh),
        is_issue_link,
        code_url=lambda tracker: cast(GitHubTracker, tracker).code_url(),
    ),
)
TRACKERS.register(
    "gitlab",
    TrackerKind(
        lambda spec, tickets, machine: GitLabTracker(spec, tickets, command=machine.glab),
        gitlab.is_issue_link,
        lambda tracker, remotes, repo: cast(GitLabTracker, tracker).preflight(remotes, repo),
        _gitlab_warnings,
        lambda tracker: cast(GitLabTracker, tracker).code_url(),
    ),
)
TRACKERS.register(
    "local",
    TrackerKind(
        lambda spec, tickets, machine: LocalTracker(Path(spec)),
        folder=lambda tracker: cast(LocalTracker, tracker).pull_request_folder,
    ),
)
PULL_REQUESTS.register(
    "github", PullRequestsKind(is_github_remote, lambda url, m: GitHubPullRequests.of_remote(url, m.gh))
)
PULL_REQUESTS.register(
    "gitlab", PullRequestsKind(is_gitlab_remote, lambda url, m: GitLabPullRequests.of_remote(url, m.glab))
)


def make_sandbox(choice: SandboxChoice) -> Sandbox:
    return SANDBOXES[choice.kind].make(choice.settings)


def make_workbench(kind: str) -> Workbench:
    return WORKBENCHES[kind]()


def _model_names(executor: Executor) -> ModelNames:
    return ModelNames(executor.default_model, executor.model_problem)


def kinds(executor: Executor | None = None) -> Kinds:
    """Returns the kind names the project file may use, from the registries.

    An `executor` that replaces the registered ones also tells the model names of every Executor kind.
    """
    return Kinds(
        executors=EXECUTORS.names(),
        sandboxes=SANDBOXES.items(),
        workbenches=WORKBENCHES.names(),
        code_hosts=PULL_REQUESTS.names(),
        models={name: _model_names(executor or EXECUTORS[name]()) for name in EXECUTORS.names()},
    )


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
        return kinds(self.executor)

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

    def _served(self, url: str, code_host: str | None) -> PullRequests | None:
        """Makes the PullRequests that serve a git remote URL, of the first kind whose rule accepts the URL or else of
        the kind the project file names, or returns None when there is none."""
        names = [name for name in PULL_REQUESTS.names() if PULL_REQUESTS[name].serves(url)]
        for name in names or ([code_host] if code_host else []):
            try:
                return PULL_REQUESTS[name].make(url, self)
            except TrackerError:
                continue
        return None

    def pull_requests_of(self, project: Project, spec: str, tracker: Tracker) -> PullRequests | PullRequestFolder:
        """Picks where the Run lands from the git remotes of the Target project, apart from the Tracker.

        A remote that points at the repository of the Spec wins,
        else `origin` or the first remote by name that a kind serves.
        Without such a remote a Spec on a code host lands in its own repository, so the landing reports the remote
        that is missing, and any other Spec keeps its PR in a folder.
        """
        tracker_kind = self.tracker_kind(spec)
        home = tracker_kind.code_url(tracker) if tracker_kind.code_url else None
        remotes = sorted(HostGit(project.repo).remotes().items(), key=lambda remote: (remote[0] != "origin", remote[0]))
        served = [pr for _, url in remotes if (pr := self._served(url, project.code_host))]
        if chosen := next((pr for pr in served if home and pr.accepts(home)), served[0] if served else None):
            return chosen
        if home and (fallback := self._served(home, project.code_host)):
            return fallback
        if tracker_kind.folder is None:
            raise TrackerError(
                "no git remote of the project is served by a kind of PullRequests, and the Spec has none;"
                " name the code host with `code_host` in the project file"
            )
        return tracker_kind.folder(tracker)

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
        frozen = RunDir(self.home / "runs" / run_id).library
        if not frozen.is_dir():
            return self.library
        return lambda repo: Library(
            [Layer("run", frozen)], kit_skills=frozenset(Kit().skills), resolved=self.home / "resolved"
        )

    @property
    def journal_path(self) -> Path:
        return self.home / "factory.db"
