"""LocalTracker reads a Spec from a local folder, and PullRequestFolder keeps the PRs of a Run whose project has no
git remote to land through as files of that folder."""

from collections.abc import Sequence
from pathlib import Path

import yaml

from factory.core import front_matter
from factory.core.states import TicketState
from factory.ports.kind import Kind, Tools
from factory.ports.tracker import (
    CiStatus,
    Issue,
    PullRequest,
    SpecLink,
    SpecSnapshot,
    TicketSnapshot,
    TrackerError,
    merged_title,
    not_landed,
)


class LocalTracker:
    """LocalTracker reads a Spec from a local folder.

    The folder holds `spec.md`, which starts with a `# Title` line, and one `tickets/<id>.md` per Ticket.
    A Ticket file may start with YAML front matter holding `title` and `blocked_by`.
    Without `tickets` the Run takes every Ticket of the folder, and with it only those ids,
    each blocked by none but Tickets the Run takes.
    A Run's projection lands in `projection/<id>/`: `comments/<key>.md` per comment and `labels`, one label per line.
    The issues a Run files land in `issues/<id>.md`, with `title`, `labels`, `milestone` and `state` in the front
    matter and the body after it, and the id of an issue is a number one past the highest before it.
    """

    def __init__(self, folder: Path, tickets: Sequence[str] | None = None) -> None:
        self.folder = folder
        self.selected = list(dict.fromkeys(tickets)) if tickets is not None else None

    def code_url(self) -> None:
        return None

    @property
    def pull_request_folder(self) -> Path:
        """Returns the folder that keeps the Run's PR when no git remote serves it."""
        return self.folder / "pull_requests"

    def reference(self, ticket: str) -> str:
        """Shows a Ticket or issue by its bare id, which names its file."""
        return ticket

    def describe(
        self, snapshot: SpecSnapshot, run_id: str, states: dict[str, TicketState], kept: dict[str, str]
    ) -> PullRequest:
        intro = f"Lands Spec {snapshot.title} from Run {run_id}."
        return PullRequest(
            title=merged_title(snapshot, states), body=f"{intro}\n\n{self.footer(snapshot, states, kept)}"
        )

    def footer(self, snapshot: SpecSnapshot, states: dict[str, TicketState], kept: dict[str, str]) -> str:
        merged = [t for t in snapshot.tickets if states[t.id] == TicketState.MERGED]
        return (
            "Merged:\n\n"
            + "".join(f"- {self.reference(t.id)} {t.title}\n" for t in merged)
            + not_landed(snapshot, states, kept, self.reference)
        )

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

    def find_issues(self, labels: Sequence[str]) -> list[Issue]:
        found = []
        for path in sorted((self.folder / "issues").glob("*.md"), key=lambda path: int(path.stem)):
            try:
                meta, body = front_matter.split(path.read_text())
            except front_matter.FrontMatterError as error:
                raise TrackerError(f"{path}: {error}") from error
            if meta.get("state", "open") == "open" and set(labels) <= {str(label) for label in meta.get("labels", [])}:
                found.append(Issue(path.stem, str(meta.get("title", path.stem)), body.strip()))
        return found

    def create_issue(self, title: str, body: str, labels: Sequence[str], milestone: str | None) -> str:
        folder = self.folder / "issues"
        folder.mkdir(exist_ok=True)
        number = max((int(path.stem) for path in folder.glob("*.md")), default=0) + 1
        meta = {
            "title": title,
            "labels": list(labels),
            "state": "open",
            **({"milestone": milestone} if milestone else {}),
        }
        (folder / f"{number}.md").write_text(f"---\n{yaml.safe_dump(meta)}---\n{body}\n")
        return str(number)

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
        paths = {path.stem: path for path in sorted((self.folder / "tickets").glob("*.md"))}
        if not paths:
            raise TrackerError(f"{self.folder} has no Tickets in tickets/")
        if self.selected is None:
            return SpecSnapshot(str(self.folder), title, body, [self._read_ticket(path) for path in paths.values()])
        for ticket in self.selected:
            if ticket not in paths:
                raise TrackerError(f"{ticket} is not a Ticket of {self.folder}")
        tickets = [self._read_ticket(paths[ticket]) for ticket in self.selected]
        for taken in tickets:
            if outside := [other for other in taken.blocked_by if other not in self.selected]:
                raise TrackerError(f"{taken.id} is blocked by {', '.join(outside)}, which this Run does not take")
        return SpecSnapshot(str(self.folder), title, body, tickets)

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


class PullRequestFolder:
    """PullRequestFolder writes each PR of a Run as a file of its folder, which no code host serves and no CI reads.

    A file holds the PR's title, its branches and its body, and the URL of the PR is the file's path.
    """

    pushes = False
    ci_config_paths: tuple[str, ...] = ()

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def accepts(self, remote_url: str) -> bool:
        """Accepts no git remote, because the PRs stay on the machine."""
        return False

    def url(self) -> str:
        return str(self.folder)

    def open(self, head: str, base: str, title: str, body: Path, layer: int | None = None) -> str:
        """Writes the PR's title, branches and body into its file and returns the file's path.

        The file of a layer in a stack of PRs starts with its number, so the files sort in stack order.
        """
        prefix = f"{layer:02d}-" if layer is not None else ""
        path = self.folder / f"{prefix}{head.replace('/', '--')}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"# {title}\n\nFrom `{head}` into `{base}`.\n\n{body.read_text()}")
        return str(path)

    def open_command(self, head: str, base: str, title: str, body: Path) -> list[str]:
        """Returns no command, because the factory writes the file on the machine and nothing is left to do by hand."""
        return []

    def edit(self, pr: str, body: Path) -> None:
        """Rewrites the body of the PR's file and keeps its title and branches."""
        title, branches = Path(pr).read_text().split("\n\n", 2)[:2]
        Path(pr).write_text(f"{title}\n\n{branches}\n\n{body.read_text()}")

    def head(self, pr: str) -> str:
        raise TrackerError(f"{pr} is a PR file, whose branch no code host holds")

    def ci(self, pr: str, commit: str) -> CiStatus:
        return CiStatus("absent")


def _tracker(link: SpecLink, tools: Tools) -> LocalTracker:
    """Makes the LocalTracker of a Spec folder, which needs no tool of the Machine."""
    return LocalTracker(Path(link.spec), link.tickets)


def _pull_requests(folder: str, tools: Tools) -> PullRequestFolder:
    """Makes the PullRequestFolder of a folder, which needs no tool of the Machine."""
    return PullRequestFolder(Path(folder))


TRACKER = Kind[LocalTracker](_tracker)
PULL_REQUESTS = Kind[PullRequestFolder](_pull_requests)
