"""Sandbox port: the environment where an agent or a Probe works on a Workspace."""

import subprocess
from pathlib import Path
from typing import Protocol


class Process(Protocol):
    def poll(self) -> int | None: ...


class Box(Protocol):
    """One Ticket's Sandbox, which runs every Attempt and Probe of that Ticket."""

    def exec(self, argv: list[str], stdout: Path) -> Process: ...


class Sandbox(Protocol):
    def create(self, name: str, workspace: Path) -> Box: ...


class LocalBox:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace

    def exec(self, argv: list[str], stdout: Path) -> Process:
        with stdout.open("wb") as out:
            return subprocess.Popen(argv, cwd=self._workspace, stdin=subprocess.DEVNULL, stdout=out)


class LocalSandbox:
    """Runs processes straight in the Workspace on the host, with no isolation."""

    def create(self, name: str, workspace: Path) -> Box:
        return LocalBox(workspace)
