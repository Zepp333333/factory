"""The Workbench port creates the Workspaces the owner watches and shows the owner the Run."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

# The log of an Attempt's terminal, which sits next to the Attempt's `events.ndjson`.
TERMINAL_LOG = "terminal.log"


def marker(log: Path, name: str) -> Path:
    """Returns the file `<log>.stop`, which asks the renderer to finish, or `<log>.done`, which says it has."""
    return log.with_name(f"{log.name}.{name}")


class WorkbenchError(Exception):
    pass


@dataclass(frozen=True)
class TicketTree:
    """A TicketTree is one Ticket of a RunTree: its TicketState `state`, its `node`, if any, and its `round`."""

    id: str
    state: str
    node: str | None
    round: int


@dataclass(frozen=True)
class RunTree:
    """A RunTree is a Run as a Workbench shows it, in the words of no vendor: its RunState `state`, `doing`, the text
    of what it does now, its Tickets, and in `waiting` the slot of each Decision that waits for the owner."""

    id: str
    state: str
    doing: str
    tickets: tuple[TicketTree, ...]
    waiting: tuple[str, ...]


@dataclass
class OwnerInput:
    """OwnerInput is what the owner types on the terminal of the Run's process: `stdin` and the bytes taken from it
    that no answer has used yet, where a Workbench that reads replies on that `stdin` leaves what else it reads."""

    stdin: TextIO | None
    typed: bytes = b""


class AttemptTerminal(Protocol):
    def close(self) -> None:
        """Ends the terminal of an Attempt once its agent has ended, after it showed the last lines."""
        ...

    def alive(self) -> bool:
        """Tells whether the terminal is still there, for reconciliation."""
        ...


class Workbench(Protocol):
    def create_workspace(self, repo: Path, path: Path, branch: str) -> None: ...

    def remove_workspace(self, repo: Path, path: Path) -> None:
        """Removes a Workspace that holds no uncommitted edits, and raises WorkbenchError otherwise."""
        ...

    def has_workspace(self, repo: Path, path: Path) -> bool:
        """Tells whether the Workspace at `path` is still there, for reconciliation."""
        ...

    def mark_stage(self, path: Path, text: str) -> None:
        """Shows `text`, the Ticket's current Stage, on the card of the Workspace at `path`."""
        ...

    def open_attempt(self, events: Path, log: Path, workspace: Path, title: str) -> AttemptTerminal:
        """Opens the terminal where the owner follows the Attempt whose Executor writes its Attempt events to `events`.

        The terminal shows the Attempt's lines and keeps them in `log`.
        A Workbench that ties terminals to Workspaces opens it on `workspace` and names it `title`.
        """
        ...

    def show_run(self, tree: RunTree) -> None:
        """Shows the owner `tree`, each time the Run may have changed, so it shows only what is new."""
        ...

    def needs_owner(self, on: bool) -> None:
        """Tells the owner, with `on`, that a Decision has started to wait for them, and without it that none waits."""
        ...
