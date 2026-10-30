"""The Workbench port creates the Workspaces the owner watches."""

import contextlib
import json
import shlex
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from factory import git, spawn

# The log of an Attempt's terminal, which sits next to the Attempt's `stream.ndjson`.
TERMINAL_LOG = "terminal.log"

# `close` waits this long, in seconds, for the renderer to write the last lines before it kills it.
RENDERER_GRACE_SECONDS = 5

# Orca discovers a new worktree in about half a second, and `create_workspace` waits for it this long, in seconds.
ORCA_DISCOVERY_SECONDS = 30


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

    def open_attempt(self, stream: Path, log: Path, workspace: Path, title: str, executor: str) -> AttemptTerminal:
        """Opens the terminal where the owner follows the Attempt whose agent writes `stream`.

        The terminal shows the Attempt's lines and keeps them in `log`.
        `executor` is the name of the Executor that wrote the stream, which the renderer needs to read it.
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
        """Asks the renderer to finish and waits for it, and kills it if it does not.

        Closing the renderer's stdin wakes it, so it sees the stop at once.
        """
        marker(self._log, "stop").touch()
        if self._popen.stdin is not None:
            self._popen.stdin.close()
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

    def open_attempt(self, stream: Path, log: Path, workspace: Path, title: str, executor: str) -> AttemptTerminal:
        popen = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "factory.render",
                str(stream),
                "--executor",
                executor,
                "--log",
                str(log),
                "--wake-on-stdin",
            ],
            stdin=subprocess.PIPE,
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
        """Tells whether Orca lists the terminal, apart from one it has closed and lists as orphaned for a while."""
        listed = json.loads(self._orca.call("terminal", "list", "--worktree", _selector(self._workspace), "--json"))
        terminals = listed["result"]["terminals"]
        return any(terminal["handle"] == self._handle and not terminal.get("orphaned") for terminal in terminals)

    def close(self) -> None:
        """Asks the renderer to finish and waits for its terminal to exit, and closes the terminal if it does not.

        Orca closes a terminal some time after it answers `terminal close`, so `close` waits for that too.
        """
        marker(self._log, "stop").touch()
        timeout = str(int(RENDERER_GRACE_SECONDS * 1000))
        try:
            self._orca.call("terminal", "wait", "--terminal", self._handle, "--for", "exit", "--timeout-ms", timeout)
            return
        except WorkbenchError:
            pass
        with contextlib.suppress(WorkbenchError):
            self._orca.call("terminal", "close", "--terminal", self._handle, "--tab", "--json")
            deadline = time.monotonic() + RENDERER_GRACE_SECONDS
            while self.alive() and time.monotonic() < deadline:
                time.sleep(0.1)


class OrcaWorkbench(TerminalWorkbench):
    """OrcaWorkbench shows each Workspace and Attempt in Orca through the `orca` CLI, which talks to the running app.

    `docs/research/orca-integration.md` records the commands.
    `orca worktree create` takes neither a path nor a branch, and a Workspace needs both,
    so the Workbench adds the `git worktree` itself and has Orca show worktrees made outside it.
    Orca then shows the Stage on the card of the Workspace, and the Attempt's renderer runs in an Orca terminal.
    Orca types a terminal's command into a shell that outlives it,
    so the renderer replaces that shell with `exec`, and the terminal ends with the Attempt.
    """

    def __init__(self, command: Sequence[str] = ("orca",)) -> None:
        self.command = tuple(command)
        self._repos: set[Path] = set()

    def call(self, *args: str) -> str:
        """Runs `orca` with `args` and returns its output, or raises WorkbenchError when it fails."""
        try:
            done = spawn.run([*self.command, *args])
        except OSError as error:
            raise WorkbenchError(f"orca did not start: {error}") from error
        if done.returncode != 0:
            raise WorkbenchError(f"orca {' '.join(args[:2])} failed: {(done.stderr or done.stdout).strip()}")
        return done.stdout

    def create_workspace(self, repo: Path, path: Path, branch: str) -> None:
        """Adds the worktree and waits until Orca has discovered it, because Orca refuses a worktree it has not seen."""
        self._show_external_worktrees(repo)
        super().create_workspace(repo, path, branch)
        deadline = time.monotonic() + ORCA_DISCOVERY_SECONDS
        while True:
            try:
                self.call("worktree", "show", "--worktree", _selector(path), "--json")
                return
            except WorkbenchError:
                if time.monotonic() > deadline:
                    raise
            time.sleep(0.1)

    def remove_workspace(self, repo: Path, path: Path) -> None:
        """Removes the worktree with git, which refuses a Workspace with edits, and then has Orca drop its card."""
        super().remove_workspace(repo, path)
        with contextlib.suppress(WorkbenchError):
            self.call("worktree", "rm", "--worktree", _selector(path), "--json")

    def has_workspace(self, repo: Path, path: Path) -> bool:
        return super().has_workspace(repo, path) and str(path.resolve()) in self.call("worktree", "ps", "--json")

    def mark_stage(self, path: Path, text: str) -> None:
        self.call("worktree", "set", "--worktree", _selector(path), "--comment", text, "--json")

    def open_attempt(self, stream: Path, log: Path, workspace: Path, title: str, executor: str) -> AttemptTerminal:
        renderer = "exec " + shlex.join(
            [sys.executable, "-m", "factory.render", str(stream), "--executor", executor, "--log", str(log), "--echo"]
        )
        created = self.call(
            "terminal", "create", "--worktree", _selector(workspace), "--title", title, "--command", renderer, "--json"
        )
        handle = json.loads(created).get("result", {}).get("terminal", {}).get("handle")
        if not isinstance(handle, str):
            raise WorkbenchError("orca terminal create returned no terminal handle")
        return OrcaTerminal(self, workspace, handle, log)

    def _show_external_worktrees(self, repo: Path) -> None:
        """Adds the repository to Orca and has it show worktrees that Orca did not make, once per repository."""
        main = _main_checkout(repo)
        if main in self._repos:
            return
        self.call("repo", "add", "--path", str(main), "--json")
        self.call("repo", "set", "--repo", _selector(main), "--external-worktree-visibility", "show", "--json")
        self._repos.add(main)


def _selector(path: Path) -> str:
    """Names the repository or worktree at `path` for `orca`, which knows each by its path with symlinks resolved."""
    return f"path:{path.resolve()}"


def _main_checkout(repo: Path) -> Path:
    """Returns the main working tree of `repo`, which may be a linked worktree that Orca does not know as a project."""
    try:
        return git.common_dir(repo).parent
    except git.GitError as error:
        raise WorkbenchError(str(error)) from error


def _git(repo: Path, *args: str) -> None:
    try:
        git.run(list(args), git.host_env(), cwd=repo)
    except git.GitError as error:
        raise WorkbenchError(str(error)) from error
