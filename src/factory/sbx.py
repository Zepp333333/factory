"""SbxSandbox runs each Ticket's agents and Probes in a Docker Sandbox, the microVM of the `sbx` CLI.

`docs/research/sbx-prototype.md` records the `sbx` behavior this module relies on.
Every path the Sandbox sees sits at its host path, so a path in a Brief means the same on both sides.
The agent reaches the owner's subscription through the `sbx` proxy, which holds the OAuth on the host,
and the real token never enters the VM.
"""

import contextlib
import json
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.sandbox import Mount, SandboxError, SandboxLost, SandboxSettings

TEMPLATE = "docker.io/docker/sandbox-templates@sha256:4a94d9144a70df744fd9247e0c365a14cfb97024afab8db7597c8ba3e0637642"
CPUS = 4
MEMORY = "8g"

# The agent of the `claude` kit needs only the API, because the proxy on the host refreshes the OAuth.
CLAUDE_HOSTS = ("api.anthropic.com",)

# A host no allow rule names, so the policy that admits it admits every host.
UNLISTED_HOST = "egress-check.factory.invalid"

# `uv run` would otherwise build a Linux `.venv` in the Workspace, which breaks `uv` on the host.
VENV = "/home/agent/venv"

# `claude` refuses to start without some token, and the proxy replaces whatever it sends.
OAUTH_PLACEHOLDER = "sbx-proxy-managed"

EXIT_CODE = "exit-code"
KILL_TIMEOUT = 10.0

# The script runs as a session leader through `setsid`, so `kill 0` reaches the command and all its children.
# The command reads /dev/null, so only the watchdog reads stdin, and it takes the pipe through fd 3
# because sh gives a background job /dev/null as stdin.
# The host holds the only write end of stdin, and however the host side ends, the watchdog sees EOF
# and kills the whole group.
# `sbx` passes the owner's GitHub token into the VM, and nothing in a Sandbox needs it.
WATCHDOG = """unset GH_TOKEN
exec 3<&0
"$@" </dev/null & child=$!
(cat <&3 >/dev/null; kill -KILL 0) &
wait "$child"; code=$?
echo "$code" > "$FACTORY_EXIT_CODE"
trap "" TERM
kill -TERM 0 2>/dev/null
exit "$code"
"""


class SbxProcess:
    """An SbxProcess is one `sbx exec` on the host and the command it runs in the VM.

    The command leaves its exit code in a file, because the code of `sbx exec` is the same
    whether the command exited or the Sandbox died under it.
    """

    def __init__(self, popen: subprocess.Popen[bytes], stdin: int, exit_code: Path, status: Callable[[], str]) -> None:
        self._popen = popen
        self._stdin: int | None = stdin
        self._exit_code = exit_code
        self._status = status
        self._lost: str | None = None
        self._killed = False

    def poll(self) -> int | None:
        code = self._popen.poll()
        if code is None:
            return None
        self._close()
        if self._killed:
            return code
        if self._lost is None:
            with contextlib.suppress(OSError, ValueError):
                return int(self._exit_code.read_text())
            self._lost = f"sbx exec exited with code {code} and no exit code, {self._status()}"
        raise SandboxLost(self._lost)

    def kill(self) -> None:
        """Closes stdin, which makes the watchdog in the VM kill the command's process group."""
        self._killed = True
        self._close()
        try:
            self._popen.wait(timeout=KILL_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._popen.kill()
            self._popen.wait()

    def _close(self) -> None:
        if self._stdin is not None:
            os.close(self._stdin)
            self._stdin = None


@dataclass
class _Sandbox:
    workspace: Path
    processes: list[SbxProcess]


class SbxSandbox:
    """SbxSandbox gives each Ticket one `sbx` Sandbox for the Ticket's whole life.

    Egress is denied except for the hosts of the `claude` kit and the project's `egress`.
    """

    def __init__(self, settings: SandboxSettings, command: Sequence[str] = ("sbx",)) -> None:
        self.settings = settings
        self.command = tuple(command)
        self._sandboxes: dict[str, _Sandbox] = {}

    def check(self) -> None:
        """Refuses a machine whose `sbx` has no subscription OAuth or lets Sandboxes reach every host."""
        secrets = json.loads(self._sbx("secret", "ls", "--json")).get("secrets") or []
        if not any(s.get("name") == "anthropic" and s.get("secret") == "(oauth configured)" for s in secrets):
            raise SandboxError(
                "sbx has no Claude subscription: run `sbx run claude`, sign in with /login once, "
                "then remove that Sandbox"
            )
        if self._allowed(UNLISTED_HOST):
            raise SandboxError("sbx policy allows every host: run `sbx policy init deny-all`")

    def create(self, name: str, workspace: Path, mounts: Sequence[Mount]) -> None:
        """Creates the Sandbox and its egress rules, and removes it again when a step fails."""
        template = self.settings.template or TEMPLATE

        # A template pinned by digest cannot change under a tag, and any other is the owner's own local one.
        pull = "missing" if "@sha256:" in template else "never"
        paths = [str(m.path) if m.writable else f"{m.path}:ro" for m in mounts]
        self._sbx(
            "create",
            "--name",
            name,
            "--pull",
            pull,
            "-t",
            template,
            "--cpus",
            str(self.settings.cpus or CPUS),
            "--memory",
            self.settings.memory or MEMORY,
            "--no-progress",
            "claude",
            str(workspace),
            *paths,
        )
        self._sandboxes[name] = _Sandbox(workspace=workspace, processes=[])
        try:
            self._sbx("policy", "allow", "network", "--sandbox", name, ",".join((*CLAUDE_HOSTS, *self.settings.egress)))
        except SandboxError:
            with contextlib.suppress(SandboxError):
                self.remove(name)
            raise

    def start(self, name: str) -> None:
        """Starts a stopped Sandbox with a no-op command, so `sbx` reports the start there and not to a process."""
        self._sbx("exec", "-w", "/", name, "true")

    def stop(self, name: str) -> None:
        for process in self._get(name).processes:
            process.kill()
        self._sbx("stop", name)

    def remove(self, name: str) -> str | None:
        """Deletes the Sandbox, or leaves it behind when `sbx` cannot delete a Sandbox whose VM died."""
        sandbox = self._sandboxes.pop(name, None)
        for process in sandbox.processes if sandbox else []:
            process.kill()
        try:
            self._sbx("rm", "-f", name)
        except SandboxError as error:
            status = self._status(name)
            if status is None:
                return None
            if status == "running":
                raise
            return f"{error}, and the {status} Sandbox stays behind"
        return None

    def exec(
        self, name: str, argv: list[str], stdout: Path, env: dict[str, str] | None = None, stderr: Path | None = None
    ) -> SbxProcess:
        """Starts `argv` with `sbx exec -i` under the watchdog, after starting the Sandbox if it was stopped."""
        sandbox = self._get(name)
        self.start(name)
        exit_code = stdout.parent / EXIT_CODE
        exit_code.unlink(missing_ok=True)
        variables = {
            "FACTORY_EXIT_CODE": str(exit_code),
            "UV_PROJECT_ENVIRONMENT": VENV,
            "CLAUDE_CODE_OAUTH_TOKEN": OAUTH_PLACEHOLDER,
            **(env or {}),
        }
        flags = [arg for key, value in variables.items() for arg in ("-e", f"{key}={value}")]
        command = [*self.command, "exec", "-i", "-w", str(sandbox.workspace), *flags, name]
        reader, writer = os.pipe()
        try:
            with stdout.open("wb") as out, stderr.open("wb") if stderr else contextlib.nullcontext() as err:
                popen = subprocess.Popen(
                    [*command, "setsid", "sh", "-c", WATCHDOG, "sh", *argv],
                    stdin=reader,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
        except OSError as error:
            os.close(writer)
            raise SandboxError(f"Sandbox {name} cannot start {argv[0]}: {error}") from error
        finally:
            os.close(reader)
        process = SbxProcess(popen, writer, exit_code, lambda: self._describe(name))
        sandbox.processes.append(process)
        return process

    def _get(self, name: str) -> _Sandbox:
        if name not in self._sandboxes:
            raise SandboxError(f"Sandbox {name} does not exist")
        return self._sandboxes[name]

    def _allowed(self, host: str) -> bool:
        proc = self._run("policy", "check", "network", "--json", host)
        try:
            verdict: dict[str, Any] = json.loads(proc.stdout)
        except json.JSONDecodeError as error:
            raise SandboxError(f"sbx policy check gave no verdict: {proc.stderr.strip()}") from error
        return verdict.get("allowed") is True

    def _status(self, name: str) -> str | None:
        """Returns the Sandbox's status as `sbx ls` reports it, or None when the Sandbox is gone."""
        listed = json.loads(self._sbx("ls", "--json"))
        return next((str(s.get("status")) for s in listed if s.get("name") == name), None)

    def _describe(self, name: str) -> str:
        try:
            status = self._status(name)
        except (SandboxError, json.JSONDecodeError) as error:
            return f"and sbx ls failed: {error}"
        return f"Sandbox {name} is {status}" if status else f"Sandbox {name} is gone"

    def _sbx(self, *args: str) -> str:
        proc = self._run(*args)
        if proc.returncode != 0:
            # `sbx` prints its error first and its hints after it.
            message = proc.stderr.strip().splitlines()
            raise SandboxError(f"sbx {args[0]} failed: {message[0] if message else f'exit code {proc.returncode}'}")
        return proc.stdout

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run([*self.command, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        except OSError as error:
            raise SandboxError(f"cannot run sbx: {error}") from error
