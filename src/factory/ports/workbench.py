"""The Workbench port creates the Workspaces the owner watches."""

from pathlib import Path
from typing import Protocol

# The log of an Attempt's terminal, which sits next to the Attempt's `events.ndjson`.
TERMINAL_LOG = "terminal.log"


def marker(log: Path, name: str) -> Path:
    """Returns the file `<log>.stop`, which asks the renderer to finish, or `<log>.done`, which says it has."""
    return log.with_name(f"{log.name}.{name}")


class WorkbenchError(Exception):
    pass


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
