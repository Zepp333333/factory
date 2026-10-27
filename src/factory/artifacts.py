"""Artifacts live in the Run directory outside the Target project.

Every Artifact is written once and then made read-only.
An Attempt's Artifacts live under the key (Ticket, Stage, Round, Attempt).
A Probe's Artifacts live under the key (Ticket, Gate, Probe), where Probes of a Gate count up over the whole Ticket.
"""

import json
from dataclasses import dataclass
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

    def ticket_dir(self, ticket: str) -> Path:
        """Returns the directory that holds a Ticket's rendered text and every Attempt and Probe of it."""
        return self.root / "tickets" / ticket

    def ticket(self, ticket: str) -> Path:
        return self.ticket_dir(ticket) / "ticket.md"

    @property
    def pull_request(self) -> Path:
        return self.root / "pull_request.md"

    @property
    def pull_request_title(self) -> Path:
        return self.root / "pull_request_title.txt"

    @property
    def describer(self) -> Path:
        """Returns the directory of the Describer's Attempts, which write the PR text."""
        return self.root / "describer"

    @property
    def agent_home(self) -> Path:
        return self.root / "agent"

    def attempt(self, ticket: str, stage: str, round: int, attempt: int) -> Path:
        path = self.ticket_dir(ticket) / stage / f"round-{round}" / f"attempt-{attempt}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def decision(self, ticket: str, decision: str, round: int) -> Path:
        """Returns where the outcome of a Decision's Round is written, next to the Attempts of a model Decider."""
        path = self.ticket_dir(ticket) / decision / f"round-{round}"
        path.mkdir(parents=True, exist_ok=True)
        return path / "decision.json"

    def borrowed_verdict(self, ticket: str, gate: str, source: str, probe: int) -> Path:
        """Returns where a Gate with `from` writes its verdict on Probe `probe` of its source Gate."""
        path = self.ticket_dir(ticket) / gate / f"from-{source}-probe-{probe}"
        path.mkdir(parents=True, exist_ok=True)
        return path / "verdict.json"

    def probe(self, ticket: str, gate: str, number: int) -> "ProbeDir":
        """Creates the directory of a Probe with an empty `gate_out` in it for `$GATE_OUT`."""
        probe = ProbeDir(self.ticket_dir(ticket) / gate / f"probe-{number}")
        probe.gate_out.mkdir(parents=True)
        return probe

    def render(self, snapshot: SpecSnapshot) -> dict[str, Any]:
        """Renders the Spec and each Ticket from the snapshot and returns where they went."""
        write_once(self.spec, f"# {snapshot.title}\n\n{snapshot.body}\n")
        tickets = {}
        for ticket in snapshot.tickets:
            header = [f"# {ticket.title}", *(f"Blocked by: {other}" for other in ticket.blocked_by)]
            write_once(self.ticket(ticket.id), "\n".join(header) + f"\n\n{ticket.body}\n")
            tickets[ticket.id] = str(self.ticket(ticket.id))
        return {"spec": str(self.spec), "tickets": tickets}


@dataclass(frozen=True)
class ProbeDir:
    root: Path

    @property
    def gate_out(self) -> Path:
        return self.root / "gate_out"

    @property
    def log(self) -> Path:
        return self.root / "log.txt"

    @property
    def verdict(self) -> Path:
        return self.root / "verdict.json"


def write_once(path: Path, text: str) -> Path:
    if path.exists():
        raise ArtifactExists(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o444)
    return path


def write_json_once(path: Path, data: Any) -> Path:
    return write_once(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
