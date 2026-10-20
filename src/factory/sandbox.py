"""The Sandbox port runs the agents and Probes of each Ticket in that Ticket's own Sandbox."""

import subprocess
from pathlib import Path
from typing import Protocol


class Process(Protocol):
    def poll(self) -> int | None: ...


class Sandbox(Protocol):
    def create(self, name: str, workspace: Path) -> None: ...

    def exec(self, name: str, argv: list[str], stdout: Path) -> Process: ...


class LocalSandbox:
    """LocalSandbox runs processes straight in the Workspace on the host, with no isolation."""

    def __init__(self) -> None:
        self._workspaces: dict[str, Path] = {}

    def create(self, name: str, workspace: Path) -> None:
        self._workspaces[name] = workspace

    def exec(self, name: str, argv: list[str], stdout: Path) -> Process:
        with stdout.open("wb") as out:
            return subprocess.Popen(argv, cwd=self._workspaces[name], stdin=subprocess.DEVNULL, stdout=out)
