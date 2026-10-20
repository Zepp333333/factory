"""Tracker port: where a Run reads its Spec and Tickets once at the start."""

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml


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


class Tracker(Protocol):
    def read_spec(self) -> SpecSnapshot: ...


class TrackerError(Exception):
    pass


class LocalTracker:
    """A Spec in a local folder: `spec.md` with a `# Title` line and one `tickets/<id>.md` per Ticket.

    A Ticket file may start with YAML front matter holding `title` and `blocked_by`.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = folder

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
        meta, body = _split_front_matter(path.read_text())
        return TicketSnapshot(
            id=path.stem,
            title=str(meta.get("title", path.stem)),
            body=body,
            blocked_by=[str(item) for item in meta.get("blocked_by", [])],
        )


def _split_title(text: str) -> tuple[str, str]:
    first, _, rest = text.partition("\n")
    if not first.startswith("# "):
        raise TrackerError("spec.md must start with a '# Title' line")
    return first[2:].strip(), rest.strip()


def _split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text.strip()
    meta, _, body = text[4:].partition("\n---\n")
    return yaml.safe_load(meta) or {}, body.strip()
