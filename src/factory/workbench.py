"""The Workbench port creates the Workspaces the owner watches."""

import contextlib
from pathlib import Path
from typing import Protocol

from factory import git


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
        _git(repo, "worktree", "add", "-q", str(path), branch)

    def remove_workspace(self, repo: Path, path: Path) -> None:
        """Runs `git worktree remove` without `--force`, so git refuses a Workspace with modified or untracked files.

        Ignored files go with the Workspace, and so does the run's directory of Workspaces once it is empty.
        """
        _git(repo, "worktree", "remove", str(path))
        with contextlib.suppress(OSError):
            path.parent.rmdir()


def _git(repo: Path, *args: str) -> None:
    try:
        git.run(list(args), git.host_env(), cwd=repo)
    except git.GitError as error:
        raise WorkbenchError(str(error)) from error
