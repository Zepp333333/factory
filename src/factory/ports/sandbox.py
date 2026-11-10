"""The Sandbox port runs the agents and Probes of each Ticket in that Ticket's own Sandbox."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class SandboxError(Exception):
    """A SandboxError means the Sandbox could not start, stop or remove itself or a process."""


class SandboxLost(SandboxError):
    """A SandboxLost means a process ended without leaving its exit code, because its Sandbox died under it."""


@dataclass(frozen=True)
class Credential:
    """A Credential is a token the agent reads from the variable `env`, which a Sandbox with a proxy holds for it.

    Such a Sandbox needs the `service` signed in on its host, tells `missing` when it is not,
    and sets `env` to `placeholder`, which the proxy replaces on the wire.
    The owner signs the service in by starting `agent` in a Sandbox and typing `sign_in` there, which the Sandbox
    tells as the fix, so how an agent signs in stays with its Executor.
    A Sandbox without a proxy leaves `env` to the host.
    """

    service: str
    env: str
    placeholder: str
    missing: str
    agent: str
    sign_in: str


@dataclass(frozen=True)
class Needs:
    """Needs are what a Sandbox must give: the hosts it lets `egress` reach, the `env` every `exec` carries,
    the `credentials` its proxy holds and the `agent` it hosts.

    `agent` names the agents for a Sandbox that sets each up by its name.
    Each Executor declares its own, the project file declares more, and the Sandbox applies them merged.
    """

    egress: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    credentials: tuple[Credential, ...] = ()
    agent: tuple[str, ...] = ()

    def merge(self, other: "Needs") -> "Needs":
        """Joins two Needs, and raises SandboxError when both set one variable to different values or one sets
        the variable of a credential.

        Of two credentials of one variable the first stays, so a service two Executors share is signed in once.
        """
        taken = {credential.env for credential in (*self.credentials, *other.credentials)}
        for name in (self.env.keys() & other.env.keys()) | (taken & (self.env.keys() | other.env.keys())):
            if name in taken or self.env[name] != other.env[name]:
                raise SandboxError(f"the Executor and the project file both set {name}")
        credentials: dict[str, Credential] = {}
        for credential in (*self.credentials, *other.credentials):
            credentials.setdefault(credential.env, credential)
        return Needs(
            tuple(dict.fromkeys((*self.egress, *other.egress))),
            {**self.env, **other.env},
            tuple(credentials.values()),
            tuple(dict.fromkeys((*self.agent, *other.agent))),
        )


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
