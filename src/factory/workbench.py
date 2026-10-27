"""The Workbench port creates the Workspaces the owner watches."""

import contextlib
import json
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

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

    def open_attempt(self, stream: Path, log: Path, workspace: Path, title: str) -> AttemptTerminal:
        """Opens the terminal where the owner follows the Attempt whose agent writes `stream`.

        The terminal shows the Attempt's lines and keeps them in `log`.
        A Workbench that ties terminals to Workspaces opens it on `workspace` and names it `title`.
        """
        ...


class RendererProcess:
    """A RendererProcess is the background `factory.render` of one Attempt, which writes its lines to a log."""

    def __init__(self, popen: "subprocess.Popen[bytes]", log: Path) -> None:
        self._popen = popen
        self._log = log

    def alive(self) -> bool:
        return self._popen.poll() is None

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

    def has_workspace(self, repo: Path, path: Path) -> bool:
        listing = git.run(["worktree", "list", "--porcelain"], git.host_env(), cwd=repo)
        return f"worktree {path}" in listing.splitlines()

    def mark_stage(self, path: Path, text: str) -> None:
        """Does nothing, because a plain terminal has no cards."""

    def open_attempt(self, stream: Path, log: Path, workspace: Path, title: str) -> AttemptTerminal:
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


class OrcaTerminal:
    """An OrcaTerminal is the Orca terminal in which the renderer of one Attempt runs."""

    def __init__(self, orca: "OrcaWorkbench", workspace: Path, handle: str, log: Path) -> None:
        self._orca = orca
        self._workspace = workspace
        self._handle = handle
        self._log = log

    def alive(self) -> bool:
        return self._handle in self._orca.call("terminal", "list", "--worktree", f"path:{self._workspace}", "--json")

    def close(self) -> None:
        """Asks the renderer to finish and waits for its terminal to exit, and goes on if Orca reports no exit."""
        render.marker(self._log, "stop").touch()
        with contextlib.suppress(WorkbenchError):
            self._orca.call("terminal", "wait", "--terminal", self._handle, "--for", "exit", "--timeout-ms", "5000")


class OrcaWorkbench(TerminalWorkbench):
    """OrcaWorkbench shows each Workspace and Attempt in Orca through the `orca` CLI, which talks to the running app.

    `docs/research/orca-integration.md` records the commands.
    `orca worktree create` takes neither a path nor a branch, and a Workspace needs both,
    so the Workbench adds the `git worktree` itself and has Orca show worktrees made outside it.
    Orca then shows the Stage on the card of the Workspace, and the Attempt's renderer runs in an Orca terminal.
    """

    def __init__(self, command: Sequence[str] = ("orca",)) -> None:
        self.command = tuple(command)
        self._repos: set[Path] = set()

    def call(self, *args: str) -> str:
        """Runs `orca` with `args` and returns its output, or raises WorkbenchError when it fails."""
        try:
            done = subprocess.run([*self.command, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        except OSError as error:
            raise WorkbenchError(f"orca did not start: {error}") from error
        if done.returncode != 0:
            raise WorkbenchError(f"orca {' '.join(args[:2])} failed: {(done.stderr or done.stdout).strip()}")
        return done.stdout

    def create_workspace(self, repo: Path, path: Path, branch: str) -> None:
        self._show_external_worktrees(repo)
        super().create_workspace(repo, path, branch)

    def remove_workspace(self, repo: Path, path: Path) -> None:
        """Removes the worktree with git, which refuses a Workspace with edits, and then has Orca drop its card."""
        super().remove_workspace(repo, path)
        with contextlib.suppress(WorkbenchError):
            self.call("worktree", "rm", "--worktree", f"path:{path}", "--json")

    def has_workspace(self, repo: Path, path: Path) -> bool:
        return super().has_workspace(repo, path) and str(path) in self.call("worktree", "ps", "--json")

    def mark_stage(self, path: Path, text: str) -> None:
        self.call("worktree", "set", "--worktree", f"path:{path}", "--comment", text, "--json")

    def open_attempt(self, stream: Path, log: Path, workspace: Path, title: str) -> AttemptTerminal:
        renderer = shlex.join([sys.executable, "-m", "factory.render", str(stream), "--log", str(log), "--echo"])
        created = self.call(
            "terminal", "create", "--worktree", f"path:{workspace}", "--title", title, "--command", renderer, "--json"
        )
        handle = _handle(json.loads(created))
        if handle is None:
            raise WorkbenchError("orca terminal create returned no terminal handle")
        return OrcaTerminal(self, workspace, handle, log)

    def _show_external_worktrees(self, repo: Path) -> None:
        """Adds the repository to Orca and has it show worktrees that Orca did not make, once per repository."""
        if repo in self._repos:
            return
        self.call("repo", "add", "--path", str(repo), "--json")
        self.call("repo", "set", "--repo", f"path:{repo}", "--external-worktree-visibility", "show", "--json")
        self._repos.add(repo)


def _handle(data: Any) -> str | None:
    """Finds the first `handle` in the JSON that `orca` printed, wherever it sits."""
    if isinstance(data, dict):
        if isinstance(data.get("handle"), str):
            return str(data["handle"])
        data = list(data.values())
    if isinstance(data, list):
        return next((found for item in data if (found := _handle(item))), None)
    return None


def _git(repo: Path, *args: str) -> None:
    try:
        git.run(list(args), git.host_env(), cwd=repo)
    except git.GitError as error:
        raise WorkbenchError(str(error)) from error
