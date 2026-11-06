"""A Run reads its Spec and Tickets once at the start through the Tracker port."""

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from factory.core.states import TicketState


@dataclass(frozen=True)
class SpecLink:
    """A SpecLink is what a Tracker kind makes its Tracker from: the Spec's link and the Ticket ids a Run takes,
    or None for every Ticket of the Spec.

    An id is opaque to all but the Tracker kind, which checks it.
    """

    spec: str
    tickets: list[str] | None = None


@dataclass(frozen=True)
class TicketSnapshot:
    """A TicketSnapshot is a Ticket as the Tracker read it, with the labels it carries.

    `blocked_by` holds the ids of its open blockers that the Tracker knows by an id, whether it read them or not, and
    `blocked_outside` every other open blocker, as the Tracker shows it, such as one in another repository.
    """

    id: str
    title: str
    body: str
    blocked_by: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    blocked_outside: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SpecSnapshot:
    ref: str
    title: str
    body: str
    tickets: list[TicketSnapshot]

    def carrying(self, label: Sequence[str], without: Sequence[str]) -> list[TicketSnapshot]:
        """Returns the Tickets that carry every label of `label` and none of `without`, in their order."""
        return [t for t in self.tickets if set(label) <= set(t.labels) and not set(without) & set(t.labels)]

    def plan_problems(self, taken: Sequence[str], left_out: Sequence[str]) -> list[str]:
        """Tells why a plan that takes `taken` in that order and leaves out `left_out` cannot stand, or returns nothing.

        A plan names each Ticket read once and no other, and takes a Ticket after those of its blockers it takes.
        A blocker it leaves out is no problem of the plan: the Ticket it blocks stays queued and never starts.
        """
        read = {t.id: t for t in self.tickets}
        named = [*taken, *left_out]
        problems = [f"{id} is no Ticket of the Spec" for id in dict.fromkeys(named) if id not in read]
        problems += [f"{id} is named twice" for id in dict.fromkeys(named) if named.count(id) > 1]
        problems += [f"{id} is neither taken nor left out with a reason" for id in read if id not in named]
        for at, id in enumerate(taken):
            if id in read:
                later = [other for other in taken[at + 1 :] if other in read[id].blocked_by]
                problems += [f"{id} comes before {other}, which it depends on" for other in dict.fromkeys(later)]
        return problems

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SpecSnapshot":
        return cls(
            ref=data["ref"],
            title=data["title"],
            body=data["body"],
            tickets=[TicketSnapshot(**ticket) for ticket in data["tickets"]],
        )


@dataclass(frozen=True)
class Issue:
    """An Issue is an open issue of the Tracker that `file_issues` may reuse, with its id, title and body."""

    id: str
    title: str
    body: str = ""


@dataclass(frozen=True)
class Comment:
    """A Comment is what someone wrote on a Ticket or a PR, and `by_factory` tells one the factory itself posted.

    `path`, `line` and `thread` are the place of the comment where the Tracker has one, and `id` stays the same
    between two reads.
    """

    id: str
    author: str
    body: str
    path: str | None = None
    line: int | None = None
    thread: str | None = None
    by_factory: bool = False


@dataclass(frozen=True)
class TrackerEvent:
    """A TrackerEvent is something that happened on a PR or on a Spec, which a `wait_tracker` node can wait for.

    `id` stays the same between two reads and tells the event from every other of its target.
    """

    id: str
    kind: Literal["approved", "changes_requested", "commented", "labeled"]
    author: str


class TrackerEvents(Protocol):
    """TrackerEvents is the one member a Run hears of events through, so a webhook source can be another adapter."""

    def events(self, target: str) -> list[TrackerEvent]:
        """Returns what happened on `target` so far, oldest first, or raises TrackerError.

        It holds only the comments and reviews of someone who can write to the repository, none of the factory's own
        comments, and every new label, whoever put it.
        """
        ...


@dataclass(frozen=True)
class PullRequest:
    title: str
    body: str


@dataclass(frozen=True)
class FailedJob:
    """A FailedJob is a CI job that failed, with where its page is and the tail of its log."""

    name: str
    url: str
    log: str = ""


@dataclass(frozen=True)
class CiStatus:
    """A CiStatus is what CI says about one commit of a PR.

    `none` means no check is reported on the commit yet, `pending` that some check is still going,
    `failed` that every check ended and some failed, which `jobs` lists,
    and `absent` that the PRs land where no CI runs, so the Run waits for none.
    """

    state: Literal["none", "pending", "passed", "failed", "absent"]
    jobs: list[FailedJob] = field(default_factory=list)


class PullRequests(TrackerEvents, Protocol):
    """PullRequests lands a Run's Integration branch in the base ref as a PR, on the code host a git remote points at
    or in a folder of the Spec.

    Its `events` take the URL of the PR.
    """

    @property
    def pushes(self) -> bool:
        """Tells whether the PR lands through a git remote, which the Run fetches its base from and pushes to."""
        ...

    @property
    def ci_config_paths(self) -> tuple[str, ...]:
        """Returns the paths of the CI config of the code host, as prefixes, which a CI fix may not change."""
        ...

    def accepts(self, remote_url: str) -> bool:
        """Tells whether a git remote points at the repository the PR opens in."""
        ...

    def url(self) -> str:
        """Returns the git URL of the repository the PR opens in."""
        ...

    def open(self, head: str, base: str, title: str, body: Path, layer: int | None = None) -> str:
        """Opens the PR and returns its URL, or raises TrackerError.

        `layer` is the number of the PR in a stack of PRs, from the bottom up.
        """
        ...

    def open_command(self, head: str, base: str, title: str, body: Path) -> list[str]:
        """Returns the command that opens the PR by hand, or none when the PR needs no code host."""
        ...

    def edit(self, pr: str, body: Path) -> None:
        """Replaces the body of the open PR with the text in `body`, or raises TrackerError."""
        ...

    def head(self, pr: str) -> str:
        """Returns the commit the PR's head branch is at now, or raises TrackerError."""
        ...

    def ci(self, pr: str, commit: str) -> CiStatus:
        """Reads the CI of `commit` on the PR; checks of any other commit do not count, so the state is `none`.

        A failed CiStatus carries the failed jobs with the tail of their logs.
        Raises TrackerError when the Tracker cannot be read.
        """
        ...

    def merged(self, pr: str) -> bool:
        """Tells whether the PR has merged, or raises TrackerError when the Tracker cannot be read."""
        ...

    def merge(self, pr: str, commit: str, method: str) -> None:
        """Merges the PR by `method`, `merge`, `squash` or `rebase`, as long as its head is at `commit`.

        Raises TrackerError when the code host refuses it or cannot be reached, and `merged` then tells which.
        """
        ...


class SpecSource(Protocol):
    """A SpecSource reads the Spec and its Tickets, and tells where the Spec lives, for `factory run`."""

    def read_spec(self) -> SpecSnapshot: ...

    def code_url(self) -> str | None:
        """Returns the git URL of the repository the Spec lives in, which a Run lands in when its project has no
        remote that tells otherwise, or None for a Spec that lives in no repository."""
        ...

    @property
    def pull_request_folder(self) -> Path | None:
        """Returns the folder that keeps the Run's PR when no git remote serves it, or None for a Tracker that
        keeps none."""
        ...


class Closing(Protocol):
    """Closing writes the text of a PR in the Tracker's words: its fallback text, the footer that closes the merged
    Tickets, the Describer's text defused, and the id of a Ticket or issue as the Tracker shows it."""

    def reference(self, ticket: str) -> str:
        """Returns how the Tracker shows the id of a Ticket or issue in a text, such as `#12` for an issue number."""
        ...

    def describe(
        self, snapshot: SpecSnapshot, run_id: str, states: dict[str, TicketState], kept: dict[str, str]
    ) -> PullRequest:
        """Writes the PR text the engine falls back on, where `kept` maps each abandoned Ticket to its work's ref."""
        ...

    def footer(self, snapshot: SpecSnapshot, states: dict[str, TicketState], kept: dict[str, str]) -> str:
        """Writes the part of the PR text that the engine appends to whatever body the Describer wrote.

        It names the merged Tickets, the way the Tracker closes them, and lists the rest with why they did not land.
        """
        ...

    def defuse(self, text: str) -> str:
        """Returns the Describer's text without the words that would close an issue in the Tracker on merge.

        Only the footer closes Tickets, so an agent cannot close an abandoned Ticket or an issue outside the Run.
        """
        ...


class TicketBoard(Protocol):
    """A TicketBoard shows the owner on each Ticket what the Run waits for: a comment and a label, for the
    Projection, and for the Tracker Operations also who works on it: the account the Tracker works as."""

    def comment(self, ticket: str, key: int, text: str) -> None:
        """Posts a comment on the Ticket; `key` is the journal event it reports, so a repeat is told from a new one."""
        ...

    def add_label(self, ticket: str, label: str) -> None: ...

    def remove_label(self, ticket: str, label: str) -> None:
        """Takes the label off the Ticket, and does nothing when it is not there."""
        ...

    def spec_id(self) -> str:
        """Returns the id of the Spec, which every member here takes in place of a Ticket's and `reference` shows."""
        ...

    def assign(self, ticket: str) -> str | None:
        """Adds the account the Tracker works as to the assignees of the Ticket, and returns that account.

        It returns None when it changed nothing, for a Ticket that account holds already or a Tracker without accounts.
        Raises TrackerError when the account does not land on the Ticket.
        """
        ...


class Discussions(TrackerEvents, Protocol):
    """Discussions reads what people wrote on a Ticket and on a PR, for a Stage that takes it as an input.

    Its `events` take the id of a Ticket or of the Spec.
    """

    def owner(self) -> str:
        """Returns the account of the owner, whose access the Tracker works with."""
        ...

    def ticket_comments(self, ticket: str) -> list[Comment]:
        """Returns the comments of the Ticket, oldest first, or raises TrackerError."""
        ...

    def pr_comments(self, pr: str) -> list[Comment]:
        """Returns the conversation and the review threads of the PR at the URL `pr`, or raises TrackerError."""
        ...


class IssueDesk(Protocol):
    """An IssueDesk finds and files the issues that the Operation `file_issues` numbers in the code."""

    def find_issues(self, labels: Sequence[str]) -> list[Issue]:
        """Returns the open issues that carry every one of `labels`; raises TrackerError when it cannot read them."""
        ...

    def create_issue(self, title: str, body: str, labels: Sequence[str], milestone: str | None) -> str:
        """Files an issue with the labels and the milestone, named by its title, and returns its id.

        Raises TrackerError when it cannot file the issue, or when the milestone does not exist.
        """
        ...


@dataclass(frozen=True)
class Tracker:
    """A Tracker is the roles a Tracker kind serves, which it declares by the ones it fills.

    Every kind reads its Spec and closes its Tickets, while a `board`, a `desk` and `discussions` are for a kind that
    can label and comment, file issues, and read comments.
    Each consumer takes only its role: `factory run` the SpecSource, the landing the Closing,
    the Projection and the Tracker Operations the TicketBoard, `file_issues` the IssueDesk,
    and a Stage with a Tracker input the Discussions.
    A Run without a TicketBoard shows its Decisions only in the terminal,
    a Pipeline or Run graph with a Tracker Operation fails `factory check` on a Tracker without one,
    a Pipeline with `file_issues` on a Tracker without an IssueDesk,
    and a Pipeline or Run graph with a Tracker input on a Tracker without Discussions.
    """

    spec: SpecSource
    closing: Closing
    board: TicketBoard | None = None
    desk: IssueDesk | None = None
    discussions: Discussions | None = None


class TrackerError(Exception):
    pass


def refuse_blocked_outside(snapshot: SpecSnapshot, reference: Callable[[str], str]) -> None:
    """Raises TrackerError for a Ticket with an open blocker the Tracker did not read, which `reference` shows."""
    read = {t.id for t in snapshot.tickets}
    for ticket in snapshot.tickets:
        outside = [reference(other) for other in ticket.blocked_by if other not in read]
        for blocker in [*outside, *ticket.blocked_outside]:
            raise TrackerError(f"{reference(ticket.id)} is blocked by open {blocker}, which this Run does not take")


def merged_title(snapshot: SpecSnapshot, states: dict[str, TicketState]) -> str:
    """Returns the fallback PR title: the Spec's title with the titles of the Tickets the Run merged, at most three."""
    merged = [t.title for t in snapshot.tickets if states[t.id] == TicketState.MERGED]
    shown = ", ".join(merged[:3]) + (f" and {len(merged) - 3} more" if len(merged) > 3 else "")
    return f"{snapshot.title}: {shown}" if shown else snapshot.title


def not_landed(
    snapshot: SpecSnapshot, states: dict[str, TicketState], kept: dict[str, str], reference: Callable[[str], str]
) -> str:
    """Lists the Tickets that did not merge with why, or returns an empty text when every Ticket did.

    `reference` shows each Ticket id the way the Tracker does.
    """
    rest = [t for t in snapshot.tickets if states[t.id] != TicketState.MERGED]
    if not rest:
        return ""
    return "\nNot landed:\n\n" + "".join(
        f"- {reference(t.id)} {t.title}: {_why(snapshot, t, states, kept, reference)}\n" for t in rest
    )


def _blocked_by(snapshot: SpecSnapshot, ticket: str) -> list[str]:
    """Returns the Tickets that depend on `ticket` directly or through others, in the Spec's order."""
    found = {ticket}
    grown = True
    while grown:
        grown = False
        for other in snapshot.tickets:
            if other.id not in found and found.intersection(other.blocked_by):
                found.add(other.id)
                grown = True
    return [t.id for t in snapshot.tickets if t.id in found and t.id != ticket]


def _why(
    snapshot: SpecSnapshot,
    ticket: TicketSnapshot,
    states: dict[str, TicketState],
    kept: dict[str, str],
    reference: Callable[[str], str],
) -> str:
    state = states[ticket.id]
    blocking = ""
    if state == TicketState.ABANDONED and (blocked := _blocked_by(snapshot, ticket.id)):
        blocking = f", blocking {', '.join(map(reference, blocked))}"
    if ticket.id in kept:
        return (
            f"{state}{blocking}, its work is in `{kept[ticket.id]}`,"
            " a ref in the repository of whoever ran the Run, not pushed"
        )
    if state == TicketState.ABANDONED:
        return f"{state}{blocking}"
    if state != TicketState.QUEUED:
        return str(state)
    waiting = [reference(other) for other in ticket.blocked_by if states.get(other) != TicketState.MERGED]
    waiting += ticket.blocked_outside
    return f"not started, blocked by {', '.join(waiting)}" if waiting else "not started"
