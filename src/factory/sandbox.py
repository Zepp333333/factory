"""The Sandbox port runs the agents and Probes of each Ticket in that Ticket's own Sandbox."""

import contextlib
import os
import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

SandboxKind = Literal["local", "sbx"]


class SandboxError(Exception):
    """A SandboxError means the Sandbox could not start, stop or remove itself or a process."""


class SandboxLost(SandboxError):
    """A SandboxLost means a process ended without leaving its exit code, because its Sandbox died under it."""


@dataclass(frozen=True)
class SandboxSettings:
    """SandboxSettings holds the project file's `sandbox` block.

    `template`, `cpus`, `memory` and `egress` apply only to kind `sbx`.
    """

    kind: SandboxKind = "local"
    template: str = ""
    cpus: int = 0
    memory: str = ""
    egress: tuple[str, ...] = ()


@dataclass(frozen=True)
class Mount:
    """A Mount is a host path the Sandbox sees at the same path, read-only unless it is `writable`."""

    path: Path
    writable: bool = False


class Process(Protocol):
    def poll(self) -> int | None:
        """Returns the exit code once the process has ended, and raises SandboxLost when the Sandbox lost it."""
        ...

    def kill(self) -> None:
        """Stops the process and its children and returns once it has exited."""
        ...


class Sandbox(Protocol):
    def check(self) -> None:
        """Raises SandboxError when this machine cannot run Sandboxes, so a Run never starts without them."""
        ...

    def create(self, name: str, workspace: Path, mounts: Sequence[Mount]) -> None:
        """Creates a Sandbox on the Workspace that sees `mounts` too, and raises SandboxError when it cannot."""
        ...

    def attach(self, name: str, workspace: Path) -> bool:
        """Takes up the Sandbox `name` on `workspace` that this process or an earlier one created.

        Returns False when no such Sandbox is there, for reconciliation.
        """
        ...

    def start(self, name: str) -> None: ...

    def stop(self, name: str) -> None:
        """Stops every process in the Sandbox and keeps the Sandbox for a later `start`."""
        ...

    def remove(self, name: str) -> str | None:
        """Stops the Sandbox and deletes it, and raises SandboxError when it cannot.

        Returns why a Sandbox stays behind when it is stopped for good and can no longer run anything.
        """
        ...

    def exec(
        self, name: str, argv: list[str], stdout: Path, env: dict[str, str] | None = None, stderr: Path | None = None
    ) -> Process:
        """Starts `argv` in the Workspace with `env` added to its environment.

        The process writes its stdout to `stdout`, and its stderr to `stderr` when one is given.
        The directory of `stdout` must be one the Sandbox can write.
        """
        ...


class LocalProcess:
    """A LocalProcess leads its own process group, so `kill` also stops the tools the agent started."""

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self._popen = popen

    def poll(self) -> int | None:
        return self._popen.poll()

    def kill(self) -> None:
        try:
            os.killpg(self._popen.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            # macOS answers EPERM, where Linux answers ESRCH, for a group whose leader already exited.
            if self._popen.poll() is None:
                raise
        self._popen.wait()


class LocalSandbox:
    """LocalSandbox runs processes straight in the Workspace on the host, with no isolation.

    It sees the whole host, so it ignores mounts.
    It remembers every process group it started, so `stop` and `remove` stop what the agent left running.
    """

    def __init__(self) -> None:
        self._workspaces: dict[str, Path] = {}
        self._processes: dict[str, list[LocalProcess]] = {}

    def check(self) -> None:
        pass

    def create(self, name: str, workspace: Path, mounts: Sequence[Mount]) -> None:
        self._workspaces[name] = workspace
        self._processes[name] = []

    def attach(self, name: str, workspace: Path) -> bool:
        return name in self._workspaces

    def start(self, name: str) -> None:
        pass

    def stop(self, name: str) -> None:
        for process in self._processes.get(name, []):
            process.kill()
        self._processes.get(name, []).clear()

    def remove(self, name: str) -> str | None:
        self.stop(name)
        self._processes.pop(name, None)
        self._workspaces.pop(name, None)
        return None

    def exec(
        self, name: str, argv: list[str], stdout: Path, env: dict[str, str] | None = None, stderr: Path | None = None
    ) -> Process:
        if name not in self._workspaces:
            raise SandboxError(f"Sandbox {name} does not exist")
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
        process = LocalProcess(popen)
        self._processes[name].append(process)
        return process
