"""The Workbench port creates the Workspaces the owner watches."""

import subprocess
from pathlib import Path
from typing import Protocol

from factory.git import SAFE_CONFIG, host_env


class WorkbenchError(Exception):
    pass


class Workbench(Protocol):
    def create_workspace(self, repo: Path, path: Path, branch: str) -> None: ...


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
