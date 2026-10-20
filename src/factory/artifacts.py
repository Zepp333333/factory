"""Artifacts live in the Run directory outside the Target project.

Every Artifact is written once and then made read-only.
An Attempt's Artifacts live under the key (Ticket, Stage, Round, Attempt).
"""

import json
from pathlib import Path
from typing import Any

from factory.tracker import SpecSnapshot


class ArtifactExists(Exception):
    pass


class RunDir:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def spec(self) -> Path:
        return self.root / "spec.md"

    def ticket(self, ticket: str) -> Path:
        return self.root / "tickets" / ticket / "ticket.md"

    def attempt(self, ticket: str, stage: str, round: int, attempt: int) -> Path:
        path = self.root / "tickets" / ticket / stage / f"round-{round}" / f"attempt-{attempt}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def render(self, snapshot: SpecSnapshot) -> dict[str, Any]:
        """Renders the Spec and each Ticket from the snapshot and returns where they went."""
        write_once(self.spec, f"# {snapshot.title}\n\n{snapshot.body}\n")
        tickets = {}
        for ticket in snapshot.tickets:
            header = [f"# {ticket.title}", *(f"Blocked by: {other}" for other in ticket.blocked_by)]
            write_once(self.ticket(ticket.id), "\n".join(header) + f"\n\n{ticket.body}\n")
            tickets[ticket.id] = str(self.ticket(ticket.id))
        return {"spec": str(self.spec), "tickets": tickets}


def write_once(path: Path, text: str) -> Path:
    if path.exists():
        raise ArtifactExists(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o444)
    return path


def write_json_once(path: Path, data: Any) -> Path:
    return write_once(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
