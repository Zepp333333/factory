"""Fake kinds of the Tracker and PullRequests ports, defined only in `tests/`, each with real behaviour on its state.

`Notebook` is a Tracker that keeps a Spec, its Tickets and the issues filed in memory.
`CodeHost` is a code host in memory that serves one git remote, whose repository really takes the Run's pushes.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from factory.core.states import TicketState
from factory.ports.kind import Kind, Tools
from factory.ports.tracker import (
    CiStatus,
    FailedJob,
    Issue,
    PullRequest,
    SpecLink,
    SpecSnapshot,
    TicketSnapshot,
    Tracker,
    TrackerError,
    TrackerEvent,
    merged_title,
    not_landed,
)
from harness import git

# The account every fake Tracker works as, which `fake_gh` and `fake_glab` write out, as they run as their own scripts.
ACCOUNT = "factory-bot"

# The id a Notebook knows its Spec by on the TicketBoard.
SPEC_ID = "spec"


@dataclass
class Notebook:
    """A Notebook is the Tracker's own store of the Spec at `link`: its Tickets, and what a Run wrote on them."""

    link: str
    title: str
    body: str
    tickets: list[TicketSnapshot]
    comments: dict[str, list[str]] = field(default_factory=dict)
    labels: dict[str, list[str]] = field(default_factory=dict)
    issues: dict[str, tuple[Issue, tuple[str, ...]]] = field(default_factory=dict)
    assignees: dict[str, list[str]] = field(default_factory=dict)
    assign_error: str = ""
    # The number of calls of the TicketBoard that fail from now on, and what runs on each call that gets through.
    board_fails: int = 0
    on_board_call: Callable[[str, str], None] | None = None

    def kind(self) -> Kind[Tracker]:
        """Returns the Tracker kind that owns the link of this Notebook."""
        return Kind(self._make, picks=lambda spec: spec == self.link)

    def _make(self, link: SpecLink, tools: Tools) -> Tracker:
        tracker = NotebookTracker(self, link.tickets)
        return Tracker(tracker, tracker, tracker, tracker)


class NotebookTracker:
    """NotebookTracker reads and writes a Notebook, and closes a Ticket with the footer line `Closes #<id>`."""

    def __init__(self, notebook: Notebook, selected: Sequence[str] | None) -> None:
        self.notebook = notebook
        self.selected = selected

    def read_spec(self) -> SpecSnapshot:
        taken = [
            replace(t, labels=list(self.notebook.labels.get(t.id, [])))
            for t in self.notebook.tickets
            if self.selected is None or t.id in self.selected
        ]
        return SpecSnapshot(self.notebook.link, self.notebook.title, self.notebook.body, taken)

    def code_url(self) -> None:
        return None

    @property
    def pull_request_folder(self) -> None:
        return None

    def reference(self, ticket: str) -> str:
        return f"#{ticket}"

    def describe(
        self, snapshot: SpecSnapshot, run_id: str, states: dict[str, TicketState], kept: dict[str, str]
    ) -> PullRequest:
        return PullRequest(merged_title(snapshot, states), self.footer(snapshot, states, kept))

    def footer(self, snapshot: SpecSnapshot, states: dict[str, TicketState], kept: dict[str, str]) -> str:
        merged = [f"Closes #{t.id}\n" for t in snapshot.tickets if states[t.id] == TicketState.MERGED]
        return "".join(merged) + not_landed(snapshot, states, kept, self.reference)

    def defuse(self, text: str) -> str:
        return text.replace("Closes #", "Touches #")

    def spec_id(self) -> str:
        return SPEC_ID

    def _called(self, member: str, ticket: str) -> None:
        """Fails the call while `board_fails` counts down, and otherwise tells `on_board_call`, which may crash."""
        if self.notebook.board_fails > 0:
            self.notebook.board_fails -= 1
            raise TrackerError("the Tracker is down")
        if self.notebook.on_board_call is not None:
            self.notebook.on_board_call(member, ticket)

    def comment(self, ticket: str, key: int, text: str) -> None:
        self._called("comment", ticket)
        self.notebook.comments.setdefault(ticket, []).append(text)

    def add_label(self, ticket: str, label: str) -> None:
        self._called("add_label", ticket)
        labels = self.notebook.labels.setdefault(ticket, [])
        if label not in labels:
            labels.append(label)

    def remove_label(self, ticket: str, label: str) -> None:
        self._called("remove_label", ticket)
        if label in self.notebook.labels.get(ticket, []):
            self.notebook.labels[ticket].remove(label)

    def assign(self, ticket: str) -> str | None:
        self._called("assign", ticket)
        if self.notebook.assign_error:
            raise TrackerError(self.notebook.assign_error)
        assignees = self.notebook.assignees.setdefault(ticket, [])
        if ACCOUNT in assignees:
            return None
        assignees.append(ACCOUNT)
        return ACCOUNT

    def find_issues(self, labels: Sequence[str]) -> list[Issue]:
        return [issue for issue, carried in self.notebook.issues.values() if set(labels) <= set(carried)]

    def create_issue(self, title: str, body: str, labels: Sequence[str], milestone: str | None) -> str:
        id = str(len(self.notebook.issues) + 1)
        self.notebook.issues[id] = (Issue(id, title, body), tuple(labels))
        return id


@dataclass
class HostedPull:
    """A HostedPull is a PR open on a CodeHost, from its branch `head` into `base`."""

    head: str
    base: str
    title: str
    body: str
    layer: int | None
    merged: bool = False


@dataclass
class CodeHost:
    """A CodeHost serves the bare git repository at `remote` and records the PRs opened on it.

    `ci_state` is what its CI says of the commit a PR's head is at, and `failed_log` the log of the job `unit` when
    that state is `failed`.
    """

    remote: Path
    pulls: list[HostedPull] = field(default_factory=list)
    ci_state: str = "none"
    failed_log: str = ""

    def kind(self) -> Kind["HostedPullRequests"]:
        """Returns the PullRequests kind that serves the git remote of this CodeHost."""
        return Kind(lambda url, tools: HostedPullRequests(self), picks=self.serves)

    def serves(self, url: str) -> bool:
        return url == str(self.remote)

    def pull(self, pr: str) -> HostedPull:
        return self.pulls[int(pr.rsplit("/", 1)[1]) - 1]


class HostedPullRequests:
    """HostedPullRequests opens PRs on a CodeHost, whose CI config lives under `.host-ci/`."""

    pushes = True
    ci_config_paths = (".host-ci/",)

    def __init__(self, host: CodeHost) -> None:
        self.host = host

    def accepts(self, remote_url: str) -> bool:
        return self.host.serves(remote_url)

    def url(self) -> str:
        return str(self.host.remote)

    def open(self, head: str, base: str, title: str, body: Path, layer: int | None = None) -> str:
        self.host.pulls.append(HostedPull(head, base, title, body.read_text(), layer))
        return f"{self.host.remote}/pull/{len(self.host.pulls)}"

    def open_command(self, head: str, base: str, title: str, body: Path) -> list[str]:
        return ["host", "open", head, base, title, str(body)]

    def events(self, target: str) -> list[TrackerEvent]:
        return []

    def edit(self, pr: str, body: Path) -> None:
        self.host.pull(pr).body = body.read_text()

    def head(self, pr: str) -> str:
        return git(self.host.remote, "rev-parse", f"refs/heads/{self.host.pull(pr).head}")

    def merged(self, pr: str) -> bool:
        return self.host.pull(pr).merged

    def merge(self, pr: str, commit: str, method: str) -> None:
        if commit != self.head(pr):
            raise TrackerError(f"{pr} is not at {commit}")
        self.host.pull(pr).merged = True

    def ci(self, pr: str, commit: str) -> CiStatus:
        state = self.host.ci_state
        if state == "none":
            return CiStatus("none")
        if commit != self.head(pr):
            return CiStatus("pending")
        if state == "failed":
            return CiStatus("failed", [FailedJob("unit", f"{self.host.remote}/jobs/1", self.host.failed_log)])
        return CiStatus("pending" if state == "pending" else "passed")
