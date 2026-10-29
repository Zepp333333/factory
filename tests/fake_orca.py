"""Fake `orca` CLI that stands in for the Orca app and runs terminal commands as background processes on the host.

It takes its state directory first and the `orca` arguments after it.
`state.json` there holds the repositories, the comment on each worktree and the terminals,
and `calls.jsonl` records every call.
Like Orca, it sees the worktrees of the added repositories as `git` knows them,
but a worktree only once it is `DISCOVERY_SECONDS` old, and it refuses to act on a worktree it does not see yet.
`terminal create` types its `--command` into a shell that stays after the command, as Orca's terminal does,
with the output in `<handle>.out`.
`terminal close` keeps listing the terminal as orphaned, as Orca does for a while.
"""

import contextlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# A new worktree stays hidden this long, in seconds, as Orca takes a while to discover one.
# Orca takes about half a second, but a shorter wait still makes the engine look again.
DISCOVERY_SECONDS = 0.15


def flags(args: list[str]) -> dict[str, str]:
    return {a: args[i + 1] for i, a in enumerate(args) if a.startswith("--") and i + 1 < len(args)}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return (
        subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()[:1] != "Z"
    )


def main() -> int:
    root, args = Path(sys.argv[1]), sys.argv[2:]
    with (root / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps(args) + "\n")
    with (root / "state.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "state.json"
        state: dict[str, Any] = (
            json.loads(path.read_text()) if path.exists() else {"repos": [], "comments": {}, "terminals": {}}
        )
        code = command(root, state, args)
        path.write_text(json.dumps(state))
    return code


def seen(state: dict[str, Any]) -> list[str]:
    """Lists the worktrees of the added repositories that Orca has had time to discover."""
    paths = []
    for repo in state["repos"]:
        out = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo, capture_output=True, text=True)
        for line in out.stdout.splitlines():
            path = line.removeprefix("worktree ")
            if line.startswith("worktree ") and (
                path == repo or time.time() - (Path(path) / ".git").stat().st_mtime >= DISCOVERY_SECONDS
            ):
                paths.append(path)
    return paths


def command(root: Path, state: dict[str, Any], args: list[str]) -> int:
    given = flags(args)
    key = tuple(args[:2])
    worktree = given.get("--worktree", "").removeprefix("path:")
    if key in (("worktree", "show"), ("worktree", "set"), ("terminal", "create")) and worktree not in seen(state):
        print(json.dumps({"ok": False, "error": {"code": "selector_not_found"}}), file=sys.stderr)
        return 1
    if key == ("repo", "add"):
        state["repos"].append(given["--path"])
    elif key == ("worktree", "set"):
        state["comments"][worktree] = given["--comment"]
    elif key == ("worktree", "ps"):
        print(json.dumps({"result": {"worktrees": [{"path": path} for path in seen(state)]}}))
    elif key == ("terminal", "create"):
        handle = f"term-{len(state['terminals']) + 1}"
        log = (root / f"{handle}.out").open("w")
        shell = f"{given['--command']}\nexec sleep 600"
        process = subprocess.Popen(["sh", "-c", shell], stdout=log, stderr=log, start_new_session=True)
        state["terminals"][handle] = {"pid": process.pid, "worktree": given["--worktree"], "title": given["--title"]}
        print(json.dumps({"result": {"terminal": {"handle": handle}}}))
    elif key == ("terminal", "list"):
        listed = [
            {"handle": h, "orphaned": t.get("closed", False)}
            for h, t in state["terminals"].items()
            if t["worktree"] == given["--worktree"] and (alive(t["pid"]) or t.get("closed"))
        ]
        print(json.dumps({"result": {"terminals": listed}}))
    elif key == ("terminal", "wait"):
        pid = state["terminals"][given["--terminal"]]["pid"]
        deadline = time.monotonic() + int(given["--timeout-ms"]) / 1000
        while alive(pid):
            if time.monotonic() > deadline:
                print("timeout", file=sys.stderr)
                return 1
            time.sleep(0.05)
    elif key == ("terminal", "close"):
        terminal = state["terminals"][given["--terminal"]]
        terminal["closed"] = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(terminal["pid"], signal.SIGKILL)
    elif key not in (("repo", "set"), ("worktree", "rm"), ("worktree", "show")):
        print(f"unknown command {args}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
