"""Workbench port: creates the Workspaces the owner watches."""

import subprocess
from pathlib import Path
from typing import Protocol


class Workbench(Protocol):
    def create_workspace(self, repo: Path, path: Path, branch: str) -> None: ...


class TerminalWorkbench:
    """Plain terminal Workbench: a Workspace is a `git worktree` on an existing branch."""

    def create_workspace(self, repo: Path, path: Path, branch: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "worktree", "add", "-q", str(path), branch],
            cwd=repo,
            check=True,
            capture_output=True,
        )
