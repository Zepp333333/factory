"""The Sandbox port runs the agents and Probes of each Ticket in that Ticket's own Sandbox."""

import contextlib
import os
import signal
import subprocess
from pathlib import Path
from typing import Protocol


class SandboxError(Exception):
    """A SandboxError means the Sandbox could not start a process."""


class Process(Protocol):
    def poll(self) -> int | None: ...

    def kill(self) -> None:
        """Stops the process and returns once it has exited."""
        ...


class Sandbox(Protocol):
    def create(self, name: str, workspace: Path) -> None: ...

    def exec(
        self, name: str, argv: list[str], stdout: Path, env: dict[str, str] | None = None, stderr: Path | None = None
    ) -> Process:
        """Starts `argv` in the Workspace with `env` added to its environment.

        The process writes its stdout to `stdout`, and its stderr to `stderr` when one is given.
        """
        ...


class LocalProcess:
    """A LocalProcess leads its own process group, so `kill` also stops the tools the agent started."""

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self._popen = popen

    def poll(self) -> int | None:
        return self._popen.poll()

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self._popen.pid, signal.SIGKILL)
        self._popen.wait()


class LocalSandbox:
    """LocalSandbox runs processes straight in the Workspace on the host, with no isolation."""

    def __init__(self) -> None:
        self._workspaces: dict[str, Path] = {}

    def create(self, name: str, workspace: Path) -> None:
        self._workspaces[name] = workspace

    def exec(
        self, name: str, argv: list[str], stdout: Path, env: dict[str, str] | None = None, stderr: Path | None = None
    ) -> Process:
        try:
            with stdout.open("wb") as out, stderr.open("wb") if stderr else contextlib.nullcontext() as err:
                popen = subprocess.Popen(
                    argv,
                    cwd=self._workspaces[name],
                    env={**os.environ, **(env or {})},
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
        except OSError as error:
            raise SandboxError(f"Sandbox {name} cannot start {argv[0]}: {error}") from error
        return LocalProcess(popen)
