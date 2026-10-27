"""The Workbench port creates the Workspaces the owner watches."""

import contextlib
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from factory import git, render

# The log of an Attempt's terminal, which sits next to the Attempt's `stream.ndjson`.
TERMINAL_LOG = "terminal.log"

# `close` waits this long, in seconds, for the renderer to write the last lines before it kills it.
RENDERER_GRACE_SECONDS = 5


class WorkbenchError(Exception):
    pass


class AttemptTerminal(Protocol):
    def close(self) -> None:
        """Ends the terminal of an Attempt once its agent has ended, after it showed the last lines."""
        ...


class Workbench(Protocol):
    def create_workspace(self, repo: Path, path: Path, branch: str) -> None: ...

    def remove_workspace(self, repo: Path, path: Path) -> None:
        """Removes a Workspace that holds no uncommitted edits, and raises WorkbenchError otherwise."""
        ...

    def open_attempt(self, stream: Path, log: Path) -> AttemptTerminal:
        """Opens the terminal where the owner follows the Attempt whose agent writes `stream`.

        The terminal shows the Attempt's lines and keeps them in `log`.
        """
        ...


class RendererProcess:
    """A RendererProcess is the background `factory.render` of one Attempt, which writes its lines to a log."""

    def __init__(self, popen: "subprocess.Popen[bytes]", log: Path) -> None:
        self._popen = popen
        self._log = log

    def close(self) -> None:
        """Asks the renderer to finish and waits for it, and kills it if it does not."""
        render.marker(self._log, "stop").touch()
        try:
            self._popen.wait(RENDERER_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self._popen.kill()
            self._popen.wait()


class TerminalWorkbench:
    """TerminalWorkbench makes each Workspace a `git worktree` on a branch the host already created.

    It runs each Attempt's renderer as a background process with a log, which `factory attach` follows.
    """

    def open_attempt(self, stream: Path, log: Path) -> AttemptTerminal:
        popen = subprocess.Popen(
            [sys.executable, "-m", "factory.render", str(stream), "--log", str(log)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return RendererProcess(popen, log)

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
