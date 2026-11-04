"""TerminalWorkbench and OrcaWorkbench create the Workspaces as `git worktree`s and open a terminal per Attempt.

TerminalWorkbench rings BEL when a Decision waits for the owner, and OrcaWorkbench shows the Run on its card.
"""

import base64
import contextlib
import json
import os
import select
import shlex
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from factory.core import git, spawn
from factory.core.states import RunState, TicketState
from factory.ports.kind import Kind
from factory.ports.workbench import AttemptTerminal, OwnerInput, RunTree, WorkbenchError, marker

# `close` waits this long, in seconds, for the renderer to write the last lines before it kills it.
RENDERER_GRACE_SECONDS = 5

# Orca discovers a new worktree in about half a second, and `create_workspace` waits for it this long, in seconds.
ORCA_DISCOVERY_SECONDS = 30

# The terminal answers the probe with `OSC 7501;?` and the states and kinds it reads, ended by ST.
PROBE = "\x1b]7501;?\x1b\\"
PROBE_SECONDS = 0.5
_OSC = "\x1b]7501;"
_ST = "\x1b\\"
BLOCKED_QUESTION = "blocked:kind=question"

TICKET_STATES = {
    TicketState.QUEUED: "idle",
    TicketState.ACTIVE: "working",
    TicketState.MERGED: "done",
    TicketState.DONE: "done",
    TicketState.ABANDONED: "error",
}
RUN_STATES = {
    RunState.RUNNING: "working",
    RunState.DONE: "done",
    RunState.PARTIAL: "error",
    RunState.ABORTED: "error",
    RunState.ORPHANED: "error",
}


class RendererProcess:
    """A RendererProcess is the background `factory.render` of one Attempt, which writes its lines to a log."""

    def __init__(self, popen: "subprocess.Popen[bytes]") -> None:
        self._popen = popen

    def alive(self) -> bool:
        return self._popen.poll() is None

    def close(self) -> None:
        """Asks the renderer to finish by closing its stdin and waits for it, and kills it if it does not finish.

        The stdin also closes when the factory process dies, so the renderer never outlives it.
        """
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

    def show_run(self, tree: RunTree) -> None:
        """Does nothing, because the Run's process prints the text tree on a plain terminal anyway."""

    def needs_owner(self, on: bool) -> None:
        """Rings BEL when a Decision starts to wait for the owner."""
        if on:
            sys.stdout.write("\a")
            sys.stdout.flush()

    def open_attempt(self, events: Path, log: Path, workspace: Path, title: str) -> AttemptTerminal:
        popen = subprocess.Popen(
            [sys.executable, "-m", "factory.render", str(events), "--log", str(log), "--stop-on-stdin"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return RendererProcess(popen)

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
    The Run card takes the Run tree as OSC 7501 records on the terminal of the Run's process, once that terminal
    answers the probe; one that does not, or no terminal, gets what a plain terminal gets.
    """

    def __init__(self, command: Sequence[str] = ("orca",), owner_input: OwnerInput | None = None) -> None:
        self.command = tuple(command)
        self._repos: set[Path] = set()
        self._owner_input = owner_input or OwnerInput(None)
        self._osc: bool | None = None
        self._last_records = ""

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

    def open_attempt(self, events: Path, log: Path, workspace: Path, title: str) -> AttemptTerminal:
        renderer = "exec " + shlex.join(
            [sys.executable, "-m", "factory.render", str(events), "--log", str(log), "--echo"]
        )
        created = self.call(
            "terminal", "create", "--worktree", _selector(workspace), "--title", title, "--command", renderer, "--json"
        )
        handle = json.loads(created).get("result", {}).get("terminal", {}).get("handle")
        if not isinstance(handle, str):
            raise WorkbenchError("orca terminal create returned no terminal handle")
        return OrcaTerminal(self, workspace, handle, log)

    def show_run(self, tree: RunTree) -> None:
        """Writes the records of `tree` when the terminal reads them and they changed."""
        if not self._reads_records():
            return
        records = render_records(tree)
        if records != self._last_records:
            self._last_records = records
            sys.stdout.write(records)
            sys.stdout.flush()

    def needs_owner(self, on: bool) -> None:
        """Rings BEL unless the terminal reads the records, where `BLOCKED_QUESTION` shows Orca's "Needs You"."""
        if not self._reads_records():
            super().needs_owner(on)

    def _reads_records(self) -> bool:
        """Tells whether the terminal of the Run's process reads OSC 7501, which it asks once."""
        if self._osc is None:
            stdin = self._owner_input.stdin
            self._osc = stdin is not None and stdin.isatty() and self._probe(stdin)
        return self._osc

    def _probe(self, stdin: TextIO) -> bool:
        """Asks the terminal whether it reads OSC 7501; whatever else it sends back is kept as typed answers."""
        try:
            fd = stdin.fileno()
        except (OSError, ValueError):
            return False
        try:
            saved = termios.tcgetattr(fd)
        except termios.error:
            saved = None
        reply = b""
        try:
            if saved is not None:
                tty.setraw(fd)
            sys.stdout.write(PROBE)
            sys.stdout.flush()
            deadline = time.monotonic() + PROBE_SECONDS
            while _ST.encode() not in reply:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([fd], [], [], left)[0]:
                    break
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                reply += chunk
        except OSError:
            return False
        finally:
            if saved is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        head, end, rest = reply.partition(_ST.encode())
        if end and head.startswith((_OSC + "?").encode()):
            self._owner_input.typed += rest
            return True
        self._owner_input.typed += reply
        return False

    def _show_external_worktrees(self, repo: Path) -> None:
        """Adds the repository to Orca and has it show worktrees that Orca did not make, once per repository."""
        main = _main_checkout(repo)
        if main in self._repos:
            return
        self.call("repo", "add", "--path", str(main), "--json")
        self.call("repo", "set", "--repo", _selector(main), "--external-worktree-visibility", "show", "--json")
        self._repos.add(main)


def render_records(tree: RunTree) -> str:
    """Renders the tree as OSC 7501 records: the root `app=factory` is the Run, and each child `id=<ticket>` a Ticket.

    A Decision that waits for a human blocks its Ticket and the root with `kind=question`,
    because Orca shows "Needs You" by the state of the root.
    """
    root_state = BLOCKED_QUESTION if tree.waiting else RUN_STATES[RunState(tree.state)]
    lines = [_record(root_state, tree.doing, app="factory", title=f"Run {tree.id}")]
    for ticket in tree.tickets:
        state = BLOCKED_QUESTION if ticket.id in tree.waiting else TICKET_STATES[TicketState(ticket.state)]
        lines.append(_record(state, f"{ticket.node or '-'} round {ticket.round}", id=ticket.id))
    return "".join(lines)


def _record(state: str, msg: str, id: str | None = None, app: str | None = None, title: str | None = None) -> str:
    fields = [f"id={id}"] if id else []
    fields.append(f"state={state}")
    if app:
        fields.append(f"app={app}")
    if title:
        fields.append(f"title={_b64(title)}")
    fields.append(f"msg={_b64(msg)}")
    return f"{_OSC}{':'.join(fields)}{_ST}"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


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


TERMINAL = Kind[TerminalWorkbench](lambda settings, tools: TerminalWorkbench())
ORCA = Kind[OrcaWorkbench](lambda settings, tools: OrcaWorkbench(owner_input=tools.owner_input))
