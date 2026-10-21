"""The Workbench port creates the Workspaces the owner watches."""

import contextlib
import subprocess
from pathlib import Path
from typing import Protocol

from factory.git import SAFE_CONFIG, host_env


class WorkbenchError(Exception):
    pass


class Workbench(Protocol):
    def create_workspace(self, repo: Path, path: Path, branch: str) -> None: ...

    def remove_workspace(self, repo: Path, path: Path) -> None:
        """Removes a Workspace that holds no uncommitted edits, and raises WorkbenchError otherwise."""
        ...


class TerminalWorkbench:
    """TerminalWorkbench makes each Workspace a `git worktree` on a branch the host already created."""

    def create_workspace(self, repo: Path, path: Path, branch: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", *SAFE_CONFIG, "worktree", "add", "-q", str(path), branch],
            cwd=repo,
            env=host_env(),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise WorkbenchError(f"git worktree add {path}: {proc.stderr.strip()}")

    def remove_workspace(self, repo: Path, path: Path) -> None:
        """Runs `git worktree remove` without `--force`, so git refuses a Workspace with modified or untracked files.

        Ignored files go with the Workspace, and so does the run's directory of Workspaces once it is empty.
        """
        proc = subprocess.run(
            ["git", *SAFE_CONFIG, "worktree", "remove", str(path)],
            cwd=repo,
            env=host_env(),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise WorkbenchError(f"git worktree remove {path}: {proc.stderr.strip()}")
        with contextlib.suppress(OSError):
            path.parent.rmdir()
