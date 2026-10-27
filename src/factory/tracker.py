"""A Run reads its Spec and Tickets once at the start through the Tracker port."""

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from factory import front_matter
from factory.states import TicketState


@dataclass(frozen=True)
class TicketSnapshot:
    id: str
    title: str
    body: str
    blocked_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SpecSnapshot:
    ref: str
    title: str
    body: str
    tickets: list[TicketSnapshot]

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
class PullRequest:
    title: str
    body: str


class PullRequests(Protocol):
    """PullRequests lands a Run's Integration branch in the base ref as a PR in the Tracker."""

    def accepts(self, remote_url: str) -> bool:
        """Tells whether a git remote points at the repository the PR opens in."""
        ...

    def url(self) -> str:
        """Returns the git URL of the repository the PR opens in."""
        ...

    def open(self, head: str, base: str, title: str, body: Path) -> str:
        """Opens the PR and returns its URL, or raises TrackerError."""
        ...

    def open_command(self, head: str, base: str, title: str, body: Path) -> list[str]: ...


class PullRequestFolder:
    """A PullRequestFolder keeps the PRs of a Tracker that has none as files, one per Integration branch."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def record(self, head: str, base: str, title: str, body: Path) -> Path:
        """Writes the PR's title, branches and body into its file and returns the file."""
        path = self.folder / f"{head.replace('/', '--')}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"# {title}\n\nFrom `{head}` into `{base}`.\n\n{body.read_text()}")
        return path


class Tracker(Protocol):
    def read_spec(self) -> SpecSnapshot: ...

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

    def comment(self, ticket: str, key: int, text: str) -> None:
        """Posts a comment on the Ticket; `key` is the journal event it reports, so a repeat is told from a new one."""
        ...

    def add_label(self, ticket: str, label: str) -> None: ...

    def remove_label(self, ticket: str, label: str) -> None:
        """Takes the label off the Ticket, and does nothing when it is not there."""
        ...

    @property
    def pull_requests(self) -> PullRequests | PullRequestFolder:
        """Returns where the Run opens its PR, or the folder that keeps it for a Tracker without PRs."""
        ...


class TrackerError(Exception):
    pass


class LocalTracker:
    """LocalTracker reads a Spec from a local folder.

    The folder holds `spec.md`, which starts with a `# Title` line, and one `tickets/<id>.md` per Ticket.
    A Ticket file may start with YAML front matter holding `title` and `blocked_by`.
    A Run's projection lands in `projection/<id>/`: `comments/<key>.md` per comment and `labels`, one label per line.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    @property
    def pull_requests(self) -> PullRequestFolder:
        return PullRequestFolder(self.folder / "pull_requests")

    def describe(
        self, snapshot: SpecSnapshot, run_id: str, states: dict[str, TicketState], kept: dict[str, str]
    ) -> PullRequest:
        intro = f"Lands Spec {snapshot.title} from Run {run_id}."
        return PullRequest(
            title=merged_title(snapshot, states), body=f"{intro}\n\n{self.footer(snapshot, states, kept)}"
        )

    def footer(self, snapshot: SpecSnapshot, states: dict[str, TicketState], kept: dict[str, str]) -> str:
        merged = [t for t in snapshot.tickets if states[t.id] == TicketState.MERGED]
        return "Merged:\n\n" + "".join(f"- {t.id} {t.title}\n" for t in merged) + not_landed(snapshot, states, kept)

    def defuse(self, text: str) -> str:
        return text

    def comment(self, ticket: str, key: int, text: str) -> None:
        path = self._projection(ticket) / "comments" / f"{key:06d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def add_label(self, ticket: str, label: str) -> None:
        self._write_labels(ticket, [*(other for other in self._labels(ticket) if other != label), label])

    def remove_label(self, ticket: str, label: str) -> None:
        self._write_labels(ticket, [other for other in self._labels(ticket) if other != label])

    def _projection(self, ticket: str) -> Path:
        return self.folder / "projection" / ticket

    def _labels(self, ticket: str) -> list[str]:
        path = self._projection(ticket) / "labels"
        return path.read_text().splitlines() if path.is_file() else []

    def _write_labels(self, ticket: str, labels: list[str]) -> None:
        path = self._projection(ticket) / "labels"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(f"{label}\n" for label in labels))

    def read_spec(self) -> SpecSnapshot:
        spec_file = self.folder / "spec.md"
        if not spec_file.is_file():
            raise TrackerError(f"{self.folder} has no spec.md")
        title, body = _split_title(spec_file.read_text())
        tickets = [self._read_ticket(path) for path in sorted((self.folder / "tickets").glob("*.md"))]
        if not tickets:
            raise TrackerError(f"{self.folder} has no Tickets in tickets/")
        return SpecSnapshot(ref=str(self.folder), title=title, body=body, tickets=tickets)

    def _read_ticket(self, path: Path) -> TicketSnapshot:
        try:
            meta, body = front_matter.split(path.read_text())
        except front_matter.FrontMatterError as error:
            raise TrackerError(f"{path}: {error}") from error
        return TicketSnapshot(
            id=path.stem,
            title=str(meta.get("title", path.stem)),
            body=body.strip(),
            blocked_by=[str(item) for item in meta.get("blocked_by", [])],
        )


def _split_title(text: str) -> tuple[str, str]:
    first, _, rest = text.partition("\n")
    if not first.startswith("# "):
        raise TrackerError("spec.md must start with a '# Title' line")
    return first[2:].strip(), rest.strip()


def merged_title(snapshot: SpecSnapshot, states: dict[str, TicketState]) -> str:
    """Returns the fallback PR title: the Spec's title with the titles of the Tickets the Run merged, at most three."""
    merged = [t.title for t in snapshot.tickets if states[t.id] == TicketState.MERGED]
    shown = ", ".join(merged[:3]) + (f" and {len(merged) - 3} more" if len(merged) > 3 else "")
    return f"{snapshot.title}: {shown}" if shown else snapshot.title


def not_landed(snapshot: SpecSnapshot, states: dict[str, TicketState], kept: dict[str, str], mark: str = "") -> str:
    """Lists the Tickets that did not merge with why, or returns an empty text when every Ticket did.

    `mark` goes in front of each Ticket id, as the `#` of an issue number does.
    """
    rest = [t for t in snapshot.tickets if states[t.id] != TicketState.MERGED]
    if not rest:
        return ""
    return "\nNot landed:\n\n" + "".join(
        f"- {mark}{t.id} {t.title}: {_why(snapshot, t, states, kept, mark)}\n" for t in rest
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
    snapshot: SpecSnapshot, ticket: TicketSnapshot, states: dict[str, TicketState], kept: dict[str, str], mark: str
) -> str:
    state = states[ticket.id]
    blocking = ""
    if state == TicketState.ABANDONED and (blocked := _blocked_by(snapshot, ticket.id)):
        blocking = f", blocking {', '.join(f'{mark}{other}' for other in blocked)}"
    if ticket.id in kept:
        return (
            f"{state}{blocking}, its work is in `{kept[ticket.id]}`,"
            " a ref in the repository of whoever ran the Run, not pushed"
        )
    if state == TicketState.ABANDONED:
        return f"{state}{blocking}"
    if state != TicketState.QUEUED:
        return str(state)
    waiting = [f"{mark}{other}" for other in ticket.blocked_by if states[other] != TicketState.MERGED]
    return f"not started, blocked by {', '.join(waiting)}" if waiting else "not started"
