"""The Sandbox port runs the agents and Probes of each Ticket in that Ticket's own Sandbox."""

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from factory import spawn


class SandboxError(Exception):
    """A SandboxError means the Sandbox could not start, stop or remove itself or a process."""


class SandboxLost(SandboxError):
    """A SandboxLost means a process ended without leaving its exit code, because its Sandbox died under it."""


@dataclass(frozen=True)
class LocalSettings:
    """LocalSettings is the `sandbox` block of kind `local`, which has no field but `kind`."""


@dataclass(frozen=True)
class SandboxChoice:
    """A SandboxChoice is the project file's `sandbox` block: the kind, the settings that kind parsed
    and the `env` that every `exec` in the Sandbox carries.
    """

    kind: str
    settings: Any
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Credential:
    """A Credential is a token the agent reads from the variable `env`, which a Sandbox with a proxy holds for it.

    Such a Sandbox needs the `service` signed in on its host, tells `missing` when it is not,
    and sets `env` to `placeholder`, which the proxy replaces on the wire.
    A Sandbox without a proxy leaves `env` to the host.
    """

    service: str
    env: str
    placeholder: str
    missing: str


@dataclass(frozen=True)
class Needs:
    """Needs are what a Sandbox must give: the hosts it lets `egress` reach, the `env` every `exec` carries,
    the `credentials` its proxy holds and the `agent` it hosts.

    `agent` names the agent for a Sandbox that sets one up by its name, such as `claude` for `sbx`.
    The Executor declares its own, the project file declares more, and the Sandbox applies them merged.
    """

    egress: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    credentials: tuple[Credential, ...] = ()
    agent: str = ""

    def merge(self, other: "Needs") -> "Needs":
        """Joins two Needs, and raises SandboxError when both set one variable to different values or one sets
        the variable of a credential."""
        taken = {credential.env for credential in (*self.credentials, *other.credentials)}
        for name in (self.env.keys() & other.env.keys()) | (taken & (self.env.keys() | other.env.keys())):
            if name in taken or self.env[name] != other.env[name]:
                raise SandboxError(f"the Executor and the project file both set {name}")
        return Needs(
            tuple(dict.fromkeys((*self.egress, *other.egress))),
            {**self.env, **other.env},
            tuple(dict.fromkeys((*self.credentials, *other.credentials))),
            self.agent or other.agent,
        )


# What a kind's parser tells about a field that it rejects: the field's name and the reason.
Report = Callable[[str, str], None]


@dataclass(frozen=True)
class SandboxKind:
    """A SandboxKind owns one kind of Sandbox: the `fields` of the `sandbox` block it reads besides `kind`,
    the `parse` of those fields into the kind's own settings, and the `make` of the Sandbox from those settings.

    `enforces_egress` tells whether the Sandbox holds back every host that `egress` does not list.
    A kind that cannot enforce it refuses a project that sets `egress`, instead of ignoring the field.
    """

    fields: tuple[str, ...]
    parse: Callable[[dict[str, Any], Report], Any]
    make: Callable[[Any], "Sandbox"]
    enforces_egress: bool = False


@dataclass(frozen=True)
class Mount:
    """A Mount is a host path the Sandbox sees at the same path, read-only unless it is `writable`."""

    path: Path
    writable: bool = False


@dataclass(frozen=True)
class MemoryReport:
    """A MemoryReport is what a Sandbox tells of the memory a process used: how many processes its kernel's OOM killer
    killed while the process ran, the Sandbox's memory `limit` and the `peak` usage in bytes, if the Sandbox shows it.
    """

    oom_kills: int
    limit: str | None = None
    peak: int | None = None


@dataclass(frozen=True)
class Reaped:
    """A Reaped is a process that an earlier Attempt or Probe left in the Sandbox and `reap` killed.

    `lifetime` is how many seconds it had lived.
    """

    pid: int
    command: str
    lifetime: float


class Process(Protocol):
    def memory(self) -> MemoryReport | None:
        """Returns the memory report of the ended process, or None when the Sandbox gives none."""
        ...

    def poll(self) -> int | None:
        """Returns the exit code once the process has ended, and raises SandboxLost when the Sandbox lost it."""
        ...

    def kill(self) -> None:
        """Stops the process and its children and returns once it has exited."""
        ...


class Sandbox(Protocol):
    needs: Needs
    """What the Sandbox was told to give, the Executor's and the project's merged."""

    def require(self, needs: Needs) -> None:
        """Adds `needs` to what the Sandbox gives, and raises SandboxError when `needs` clash with what it has."""
        ...

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

    def reap(self, name: str) -> list[Reaped]:
        """Kills every process that earlier `exec` calls left in the Sandbox and returns what it killed.

        The Sandbox's own processes stay, and so does anything `exec` starts after this returns.
        Raises SandboxError when it cannot tell or cannot kill.
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
