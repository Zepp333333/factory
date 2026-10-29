"""Fake `sbx` CLI that stands in for Docker Sandboxes and runs every command straight on the host.

It takes its state directory first and the `sbx` arguments after it.
`state.json` there holds the Sandboxes, the per-Sandbox egress rules, whether the anthropic OAuth is configured,
whether the global policy allows every host, whether `rm` fails and whether a Sandbox stops when it is created
and whenever its last `setsid` command ends, and `calls.jsonl` records every call.
`crash` stops every Sandbox the way a dying microVM does,
and `oom [PEAK]` makes the OOM killer of the VM, which all Sandboxes share, count a kill, with PEAK as its peak usage.
Both are no commands of the real `sbx`.
`exec` runs the command in its `-w` directory with its `-e` variables and passes stdin through.
It runs `setsid` itself, because macOS has no such command, and records the process group,
so `stop` and `rm` can kill what runs in the Sandbox the way a stopped microVM does.
Like the real `setsid` of a process group leader, `setsid` without `-w` leaves the command running
and ends the `exec` at once with code 0.
The reap script of `SbxSandbox` reads `/proc`, so on a host without it, such as macOS, `exec` answers it with `ps`.
"""

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

INITIAL: dict[str, Any] = {
    "sandboxes": {},
    "oauth": True,
    "allow_all": False,
    "rm_fails": False,
    "idle_stop": False,
    "notice_before_json": "",
    "notice_after_json": "",
}


@contextlib.contextmanager
def state(root: Path) -> Iterator[dict[str, Any]]:
    """Locks the state for one read-modify-write."""
    with (root / "state.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "state.json"
        data = json.loads(path.read_text()) if path.exists() else json.loads(json.dumps(INITIAL))
        yield data
        path.write_text(json.dumps(data))


def options(
    args: list[str], flags: tuple[str, ...], switches: tuple[str, ...]
) -> tuple[dict[str, list[str]], list[str]]:
    """Splits leading options from the positional arguments that follow them."""
    found: dict[str, list[str]] = {}
    while args and args[0].startswith("-"):
        name = args.pop(0)
        if name in switches:
            found.setdefault(name, []).append("")
        elif name in flags:
            found.setdefault(name, []).append(args.pop(0))
        else:
            fail(f"unknown flag {name}")
    return found, args


def fail(message: str, code: int = 1) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(code)


def kill_groups(sandbox: dict[str, Any]) -> None:
    for group in sandbox.pop("groups", []):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, signal.SIGKILL)


def create(root: Path, args: list[str]) -> None:
    flags = ("--name", "--pull", "-t", "--cpus", "--memory")
    found, rest = options(args, flags, ("--no-progress",))
    agent, *paths = rest
    for path in paths:
        if not Path(path.removesuffix(":ro")).exists():
            fail(f"422 Unprocessable Entity: workspace directory {path} no longer exists on the host")
    with state(root) as data:
        name = found["--name"][0]
        data["sandboxes"][name] = {
            "status": "stopped" if data["idle_stop"] else "running",
            "agent": agent,
            "workspaces": paths,
            "template": found["-t"][0],
            "pull": found["--pull"][0],
            "cpus": int(found["--cpus"][0]),
            "memory": found["--memory"][0],
            "allow": [],
            "created": time.time(),
        }


def reap(root: Path, name: str) -> None:
    """Kills every process whose environment names the Sandbox and prints it the way the reap script does.

    Listing the environment of every process on the host takes long, so only those younger than the Sandbox are read.
    """
    with state(root) as data:
        age = time.time() - data["sandboxes"][name]["created"]
    ages = subprocess.run(["ps", "-ax", "-o", "pid=,etime="], capture_output=True, text=True, check=True).stdout
    young = [pid for pid, elapsed in map(str.split, ages.splitlines()) if seconds(elapsed) <= age + 1]
    young = [pid for pid in young if int(pid) != os.getpid()]
    if not young:
        return
    columns = ["ps", "-ww", "-o", "pid=,etime=,command=", "-p", ",".join(young)]
    listed = subprocess.run(columns, capture_output=True, text=True).stdout.splitlines()
    commands = {int(pid): (elapsed, command) for pid, elapsed, command in (line.split(maxsplit=2) for line in listed)}
    for line in subprocess.run([*columns, "-E"], capture_output=True, text=True).stdout.splitlines():
        pid, _, rest = line.split(maxsplit=2)
        if int(pid) not in commands:
            continue
        elapsed, command = commands[int(pid)]
        if f"FACTORY_SANDBOX={name}" not in rest.removeprefix(command).split():
            continue
        print(f"{pid}\t{seconds(elapsed)}\t{command}")
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(int(pid), signal.SIGKILL)


def seconds(elapsed: str) -> int:
    """Turns the `[[dd-]hh:]mm:ss` of `ps -o etime` into seconds."""
    days, _, clock = elapsed.rpartition("-")
    total = 0
    for part in clock.split(":"):
        total = total * 60 + int(part)
    return total + int(days or 0) * 86400


def exec_(root: Path, args: list[str]) -> None:
    found, rest = options(args, ("-w", "-e"), ("-i",))
    name, *argv = rest
    if argv[:2] == ["sh", "-c"] and not Path("/proc").is_dir():
        # The engine's import costs more than most calls of the fake, so only a host without `/proc` pays it.
        from factory.sbx import REAP

        if argv[2] == REAP:
            reap(root, argv[4])
            return
    with state(root) as data:
        sandbox = data["sandboxes"].get(name)
        if sandbox is None:
            fail(f"sandbox {name} not found")
        if sandbox["status"] != "running":
            sandbox["status"] = "running"
            print(f"✓ sandbox {name} — started", file=sys.stderr, flush=True)
    session = argv[0] == "setsid"
    wait = session and argv[1] == "-w"
    if session:
        argv = argv[2:] if wait else argv[1:]
    # The kernel of the VM counts its OOM kills in vmstat and the cgroup shows its peak usage, which `oom` sets.
    kernel = {"FACTORY_VMSTAT": str(root / "vmstat"), "FACTORY_MEMORY_PEAK": str(root / "peak")}
    env = {**os.environ, **kernel, **dict(item.split("=", 1) for item in found.get("-e", []))}
    stdin = None if "-i" in found else subprocess.DEVNULL
    try:
        popen = subprocess.Popen(argv, cwd=found.get("-w", ["/"])[0], env=env, stdin=stdin, start_new_session=True)
    except FileNotFoundError:
        fail(f"OCI runtime exec failed: {argv[0]} not found", 127)
    with state(root) as data:
        data["sandboxes"][name].setdefault("groups", []).append(popen.pid)
    if session and not wait:
        sys.exit(0)
    code = popen.wait()
    with state(root) as data:
        sandbox = data["sandboxes"].get(name)
        if sandbox is not None:
            sandbox["groups"] = [group for group in sandbox.get("groups", []) if group != popen.pid]
            # A real Sandbox stops after half a minute idle, which the Run's next `setsid` command may well wait.
            if data["idle_stop"] and session and not sandbox["groups"]:
                sandbox["status"] = "stopped"
    sys.exit(code % 256)


def oom(root: Path, args: list[str]) -> None:
    """Makes the OOM killer of the VM, which all fake Sandboxes share, count a kill, with `PEAK` bytes if given."""
    vmstat = root / "vmstat"
    kills = int(vmstat.read_text().split()[1]) if vmstat.exists() else 0
    vmstat.write_text(f"oom_kill {kills + 1}\n")
    if args:
        (root / "peak").write_text(f"{args[0]}\n")


def policy(root: Path, args: list[str]) -> None:
    verb, kind, *rest = args
    found, rest = options(rest, ("--sandbox",), ("--json",))
    sandbox = found.get("--sandbox", [None])[0]
    with state(root) as data:
        if verb == "allow" and kind == "network":
            if sandbox not in data["sandboxes"]:
                fail(f"sandbox {sandbox} not found")
            data["sandboxes"][sandbox]["allow"] += rest[0].split(",")
            return
        if verb == "check" and kind == "network":
            rules = data["sandboxes"].get(sandbox, {}).get("allow", []) if sandbox else []
            allowed = data["allow_all"] or rest[0] in rules
            print(json.dumps({"allowed": allowed, "target": f"{rest[0]}:443"}))
            sys.exit(0 if allowed else 1)
    fail(f"unknown policy command {verb} {kind}")


def main() -> None:
    root = Path(sys.argv[1])
    command, *args = sys.argv[2:]
    with (root / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps([command, *args]) + "\n")
    if command == "create":
        create(root, args)
    elif command == "exec":
        exec_(root, args)
    elif command == "policy":
        policy(root, args)
    elif command == "secret" and args == ["ls", "--json"]:
        with state(root) as data:
            secrets = [{"scope": "global", "type": "service", "name": "anthropic", "secret": "(oauth configured)"}]
            print(data["notice_before_json"], end="")
            print(json.dumps({"secrets": secrets if data["oauth"] else [], "custom_secrets": []}))
            if data["notice_after_json"]:
                print(data["notice_after_json"])
    elif command == "ls" and args == ["--json"]:
        with state(root) as data:
            listed = [{"name": name, "status": s["status"]} for name, s in data["sandboxes"].items()]
            print(json.dumps(listed))
    elif command == "crash":
        with state(root) as data:
            for sandbox in data["sandboxes"].values():
                kill_groups(sandbox)
                sandbox["status"] = "stopped"
    elif command == "oom":
        oom(root, args)
    elif command == "stop":
        with state(root) as data:
            sandbox = data["sandboxes"][args[0]]
            kill_groups(sandbox)
            sandbox["status"] = "stopped"
    elif command == "rm" and args[0] == "-f":
        with state(root) as data:
            sandbox = data["sandboxes"].get(args[1])
            if sandbox is None:
                fail(f"sandbox {args[1]} not found")
            kill_groups(sandbox)
            sandbox["status"] = "stopped"
            if not data["rm_fails"]:
                del data["sandboxes"][args[1]]
        if data["rm_fails"]:
            fail("500 Internal Server Error: failed to fully delete sandbox")
    else:
        fail(f"unknown command {command}")


if __name__ == "__main__":
    main()
