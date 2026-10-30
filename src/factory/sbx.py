"""SbxSandbox runs each Ticket's agents and Probes in a Docker Sandbox, the microVM of the `sbx` CLI.

`docs/research/sbx-prototype.md` records the `sbx` behavior this module relies on.
Every path the Sandbox sees sits at its host path, so a path in a Brief means the same on both sides.
The agent reaches the owner's credentials through the `sbx` proxy, which holds them on the host,
and the real token never enters the Sandbox.
What the Executor and the project file need, the hosts and the variables, comes in through `require`.
"""

import contextlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory import spawn, values
from factory.sandbox import MemoryReport, Mount, Needs, Reaped, Report, SandboxError, SandboxKind, SandboxLost

TEMPLATE = "docker.io/docker/sandbox-templates@sha256:4a94d9144a70df744fd9247e0c365a14cfb97024afab8db7597c8ba3e0637642"
CPUS = 4
MEMORY = "8g"
SIZE = re.compile(r"[1-9]\d*[mg]")

# A host no allow rule names, so the policy that admits it admits every host.
UNLISTED_HOST = "egress-check.factory.invalid"

EXIT_CODE = "exit-code"
MEMORY_REPORT = "memory-report"
KILL_TIMEOUT = 10.0

# How many characters of `sbx` output a JSON parse error quotes on each side of the failure and at the end.
JSON_CONTEXT = 100

# The script runs as a session leader through `setsid`, so `kill 0` reaches the command and all its children.
# `sbx exec` starts it as a process group leader, so `setsid` forks, and `-w` keeps the exec waiting for the script.
# Without `-w` the exec ends at once, and `sbx` cuts the script's output half a minute later.
# The command reads /dev/null, so only the watchdog reads stdin, and it takes the pipe through fd 3
# because sh gives a background job /dev/null as stdin.
# The host holds the only write end of stdin, and however the host side ends, the watchdog sees EOF
# and kills the whole group.
# `sbx` passes the owner's GitHub token into the VM, and nothing in a Sandbox needs it.
# The kernel counts the processes its OOM killer killed in /proc/vmstat, and the watchdog outlives a killed command,
# so before the exit code it leaves how many the run added, and the peak usage if the VM shows one.
# The exit code file tells the host that the report is complete.
WATCHDOG = """unset GH_TOKEN
exec 3<&0
vmstat=${FACTORY_VMSTAT:-/proc/vmstat}
peak=${FACTORY_MEMORY_PEAK:-/sys/fs/cgroup/memory.peak}
before=$(sed -n 's/^oom_kill //p' "$vmstat" 2>/dev/null)
"$@" </dev/null & child=$!
(cat <&3 >/dev/null; kill -KILL 0) &
wait "$child"; code=$?
after=$(sed -n 's/^oom_kill //p' "$vmstat" 2>/dev/null)
{ echo "oom_kills $((${after:-0} - ${before:-0}))"; sed 's/^/peak /' "$peak" 2>/dev/null; } > "$FACTORY_MEMORY_REPORT"
echo "$code" > "$FACTORY_EXIT_CODE"
trap "" TERM
kill -TERM 0 2>/dev/null
exit "$code"
"""

# Every `exec` carries `FACTORY_SANDBOX=<name>` in its environment, which its children inherit even through `setsid`.
# So the processes of earlier `exec` calls are exactly those whose environment names the Sandbox, and the Sandbox's own
# processes never do. It prints `pid`, seconds lived and command per killed process, tab-separated, then kills them.
# A process is alive for `uptime - starttime / CLK_TCK` seconds, and field 22 of its stat is the 20th after the `)`.
REAP = """name=$1
hz=$(getconf CLK_TCK)
up=$(cut -d. -f1 /proc/uptime)
for dir in /proc/[0-9]*; do
  pid=${dir#/proc/}
  [ "$pid" = "$$" ] && continue
  tr '\\0' '\\n' <"$dir/environ" 2>/dev/null | grep -qx "FACTORY_SANDBOX=$name" || continue
  stat=$(cat "$dir/stat" 2>/dev/null) || continue
  set -- ${stat##*) }
  start=${20}
  cmd=$(tr '\\0' ' ' <"$dir/cmdline" 2>/dev/null)
  printf '%s\\t%s\\t%s\\n' "$pid" "$((up - start / hz))" "$cmd"
  kill -KILL "$pid" 2>/dev/null
done
"""


class SbxProcess:
    """An SbxProcess is one `sbx exec` on the host and the command it runs in the Sandbox.

    The command leaves its exit code in a file, because the code of `sbx exec` is the same
    whether the command exited or the Sandbox died under it.
    """

    def __init__(
        self,
        popen: subprocess.Popen[bytes],
        stdin: int,
        exit_code: Path,
        status: Callable[[], str],
        report: Path,
        limit: str,
    ) -> None:
        self._popen = popen
        self._stdin: int | None = stdin
        self._exit_code = exit_code
        self._report = report
        self._limit = limit
        self._status = status
        self._lost: str | None = None
        self._killed = False

    def memory(self) -> MemoryReport | None:
        """Reads what the watchdog left of the memory: the OOM kills of the run, the Sandbox's limit and the peak."""
        try:
            fields = dict(line.split() for line in self._report.read_text().splitlines())
            return MemoryReport(
                int(fields["oom_kills"]), self._limit, int(fields["peak"]) if "peak" in fields else None
            )
        except (OSError, ValueError, KeyError):
            return None

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
        """Closes stdin, which makes the watchdog in the Sandbox kill the command's process group."""
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


@dataclass(frozen=True)
class SbxSettings:
    """SbxSettings is the `sandbox` block of kind `sbx`: the `template` image, the `cpus` and `memory` of each
    Sandbox, and the hosts `egress` lets it reach besides the Executor's own.
    """

    template: str = ""
    cpus: int = 0
    memory: str = ""
    egress: tuple[str, ...] = ()


def parse_settings(body: dict[str, Any], report: Report) -> SbxSettings:
    """Reads the fields of the `sandbox` block of kind `sbx`, and reports each one it rejects."""
    template = body.get("template", "")
    if not isinstance(template, str):
        report("template", "must be a string")
        template = ""
    cpus = 0
    if "cpus" in body:
        try:
            cpus = values.positive(body["cpus"])
        except values.Invalid as error:
            report("cpus", str(error))
    memory = body.get("memory", "")
    if "memory" in body and not (isinstance(memory, str) and SIZE.fullmatch(memory)):
        report("memory", "must be a size such as 512m or 8g")
        memory = ""
    egress = body.get("egress", [])
    if not isinstance(egress, list) or not all(isinstance(host, str) and host for host in egress):
        report("egress", "must be a list of hosts")
        egress = []
    return SbxSettings(template=template, cpus=cpus, memory=memory, egress=tuple(egress))


class SbxSandbox:
    """SbxSandbox gives each Ticket one `sbx` Sandbox for the Ticket's whole life.

    Egress is denied except for the hosts the Executor needs and the project's `egress`.
    """

    def __init__(self, settings: SbxSettings, command: Sequence[str] = ("sbx",)) -> None:
        self.settings = settings
        self.command = tuple(command)
        self._sandboxes: dict[str, _Sandbox] = {}
        self.needs = Needs(egress=settings.egress)

    def require(self, needs: Needs) -> None:
        self.needs = needs.merge(self.needs)

    def check(self) -> None:
        """Refuses a machine whose `sbx` lacks a credential the Executor needs or lets Sandboxes reach every host."""
        secrets = _json(self._sbx("secret", "ls", "--json")).get("secrets") or []
        for credential in self.needs.credentials:
            if not any(
                s.get("name") == credential.service and s.get("secret") == "(oauth configured)" for s in secrets
            ):
                raise SandboxError(f"sbx has {credential.missing}")
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
            if self.needs.egress:
                self._sbx("policy", "allow", "network", "--sandbox", name, ",".join(self.needs.egress))
        except SandboxError:
            with contextlib.suppress(SandboxError):
                self.remove(name)
            raise

    def attach(self, name: str, workspace: Path) -> bool:
        """Takes up a Sandbox `sbx` lists, which an earlier process created, and returns False when it is gone."""
        if self._status(name) is None:
            self._sandboxes.pop(name, None)
            return False
        self._sandboxes.setdefault(name, _Sandbox(workspace=workspace, processes=[]))
        return True

    def start(self, name: str) -> None:
        """Starts a stopped Sandbox with a no-op command, so `sbx` reports the start there and not to a process."""
        self._sbx("exec", "-w", "/", name, "true")

    def stop(self, name: str) -> None:
        sandbox = self._get(name)
        for process in sandbox.processes:
            process.kill()
        sandbox.processes.clear()
        self._sbx("stop", name)

    def remove(self, name: str) -> str | None:
        """Deletes the Sandbox, or leaves it behind when `sbx` cannot delete a Sandbox whose microVM died."""
        sandbox = self._sandboxes.get(name)
        for process in sandbox.processes if sandbox else []:
            process.kill()
        try:
            self._sbx("rm", "-f", name)
        except SandboxError as error:
            status = self._status(name)
            if status == "running":
                raise
            self._sandboxes.pop(name, None)
            return f"{error}, and the {status} Sandbox stays behind" if status else None
        self._sandboxes.pop(name, None)
        return None

    def reap(self, name: str) -> list[Reaped]:
        """Kills every process whose environment names the Sandbox, which only `exec` calls of this Sandbox set."""
        self._get(name)
        out = self._sbx("exec", "-w", "/", name, "sh", "-c", REAP, "sh", name)
        reaped = []
        for line in out.splitlines():
            fields = line.split("\t", 2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].lstrip("-").isdigit():
                continue
            reaped.append(Reaped(int(fields[0]), fields[2].strip(), max(float(fields[1]), 0.0)))
        return reaped

    def exec(
        self, name: str, argv: list[str], stdout: Path, env: dict[str, str] | None = None, stderr: Path | None = None
    ) -> SbxProcess:
        """Starts `argv` with `sbx exec -i` under the watchdog, after starting the Sandbox if it was stopped."""
        sandbox = self._get(name)
        self.start(name)
        exit_code = stdout.parent / EXIT_CODE
        exit_code.unlink(missing_ok=True)
        report = stdout.parent / MEMORY_REPORT
        report.unlink(missing_ok=True)
        variables = {
            "FACTORY_EXIT_CODE": str(exit_code),
            "FACTORY_MEMORY_REPORT": str(report),
            "FACTORY_SANDBOX": name,
            **{credential.env: credential.placeholder for credential in self.needs.credentials},
            **self.needs.env,
            **(env or {}),
        }
        flags = [arg for key, value in variables.items() for arg in ("-e", f"{key}={value}")]
        command = [*self.command, "exec", "-i", "-w", str(sandbox.workspace), *flags, name]
        reader, writer = os.pipe()
        try:
            with stdout.open("wb") as out, stderr.open("wb") if stderr else contextlib.nullcontext() as err:
                popen = subprocess.Popen(
                    [*command, "setsid", "-w", "sh", "-c", WATCHDOG, "sh", *argv],
                    stdin=reader,
                    stdout=out,
                    stderr=err,
                    start_new_session=True,
                )
        except BaseException as error:
            os.close(writer)
            if isinstance(error, OSError):
                raise SandboxError(f"Sandbox {name} cannot start {argv[0]}: {error}") from error
            raise
        finally:
            os.close(reader)
        process = SbxProcess(
            popen, writer, exit_code, lambda: self._describe(name), report, self.settings.memory or MEMORY
        )
        sandbox.processes.append(process)
        return process

    def _get(self, name: str) -> _Sandbox:
        if name not in self._sandboxes:
            raise SandboxError(f"Sandbox {name} does not exist")
        return self._sandboxes[name]

    def _allowed(self, host: str) -> bool:
        # `sbx policy check` exits 1 on a denied host, so only its verdict tells a denial from a failure.
        proc = self._run("policy", "check", "network", "--json", host)
        return _json(proc.stdout or proc.stderr).get("allowed") is True

    def _status(self, name: str) -> str | None:
        """Returns the Sandbox's status as `sbx ls` reports it, or None when the Sandbox is gone."""
        listed = _json(self._sbx("ls", "--json"))
        return next((str(s.get("status")) for s in listed if s.get("name") == name), None)

    def _describe(self, name: str) -> str:
        try:
            status = self._status(name)
        except SandboxError as error:
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
            return spawn.run([*self.command, *args])
        except OSError as error:
            raise SandboxError(f"cannot run sbx: {error}") from error


def _json(text: str) -> Any:
    """Parses the first JSON value of `text`, because `sbx` may print a notice after it."""
    start = len(text) - len(text.lstrip())
    try:
        value, _ = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError as error:
        around = text[max(error.pos - JSON_CONTEXT, 0) : error.pos + JSON_CONTEXT].strip()
        message = f"sbx gave no JSON at char {error.pos} of {len(text)} ({error.msg}): {around!r}"
        if error.pos + JSON_CONTEXT < len(text) - JSON_CONTEXT:
            message += f", ending with {text[-JSON_CONTEXT:].strip()!r}"
        raise SandboxError(message) from error
    return value


KIND = SandboxKind(
    fields=("template", "cpus", "memory", "egress"), parse=parse_settings, make=SbxSandbox, enforces_egress=True
)
