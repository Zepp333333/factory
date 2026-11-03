"""LocalSandbox runs the agents and Probes of each Ticket straight on the host, as the Sandbox of kind `local`."""

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from factory.core import spawn
from factory.ports.sandbox import (
    LocalSettings,
    MemoryReport,
    Mount,
    Needs,
    Process,
    Reaped,
    SandboxError,
    SandboxKind,
)


class LocalProcess:
    """A LocalProcess leads its own process group, so `kill` also stops the tools the agent started."""

    def __init__(self, popen: subprocess.Popen[bytes]) -> None:
        self._popen = popen
        self.pid = popen.pid
        self.started = time.monotonic()

    def memory(self) -> MemoryReport | None:
        return None

    def poll(self) -> int | None:
        return self._popen.poll()

    def group_alive(self) -> bool:
        """Returns whether any process is left in the group, which listing every process on the host would show."""
        self._popen.poll()
        try:
            os.killpg(self._popen.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # macOS answers EPERM for a group it cannot tell about, so only listing tells.
            pass
        return True

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
        self.needs = Needs()

    def require(self, needs: Needs) -> None:
        self.needs = self.needs.merge(needs)

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

    def reap(self, name: str) -> list[Reaped]:
        """Kills what is left in the process group of every earlier `exec`, and forgets those processes."""
        processes = self._processes.get(name, [])
        leaders = {process.pid: process for process in processes}
        if not any(process.group_alive() for process in processes):
            processes.clear()
            return []
        try:
            listing = spawn.run(["ps", "-axo", "pid=,pgid=,command="]).stdout
        except OSError as error:
            raise SandboxError(f"Sandbox {name} cannot list its processes: {error}") from error
        now = time.monotonic()
        reaped = []
        for line in listing.splitlines():
            pid, pgid, *command = line.split(maxsplit=2)
            if int(pgid) in leaders and int(pid) != os.getpid():
                reaped.append(Reaped(int(pid), command[0] if command else "", now - leaders[int(pgid)].started))
        for process in processes:
            process.kill()
        processes.clear()
        return reaped

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
                    env={**os.environ, **self.needs.env, **(env or {})},
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


LOCAL = SandboxKind(fields=(), parse=lambda body, report: LocalSettings(), make=lambda settings: LocalSandbox())
