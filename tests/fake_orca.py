"""Fake `orca` CLI that stands in for the Orca app and runs terminal commands as background processes on the host.

It takes its state directory first and the `orca` arguments after it.
`state.json` there holds the repositories, the comment on each worktree and the terminals,
and `calls.jsonl` records every call.
`worktree ps` lists the worktrees of the added repositories as `git` knows them,
and `terminal create` runs its `--command` through `sh`, with the output in `<handle>.out`.
"""

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


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


def command(root: Path, state: dict[str, Any], args: list[str]) -> int:
    given = flags(args)
    key = tuple(args[:2])
    if key == ("repo", "add"):
        state["repos"].append(given["--path"])
    elif key == ("worktree", "set"):
        state["comments"][given["--worktree"].removeprefix("path:")] = given["--comment"]
    elif key == ("worktree", "ps"):
        for repo in state["repos"]:
            out = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo, capture_output=True, text=True)
            print(out.stdout)
    elif key == ("terminal", "create"):
        handle = f"term-{len(state['terminals']) + 1}"
        log = (root / f"{handle}.out").open("w")
        process = subprocess.Popen(["sh", "-c", given["--command"]], stdout=log, stderr=log, start_new_session=True)
        state["terminals"][handle] = {"pid": process.pid, "worktree": given["--worktree"], "title": given["--title"]}
        print(json.dumps({"result": {"terminal": {"handle": handle}}}))
    elif key == ("terminal", "list"):
        print(
            json.dumps(
                [h for h, t in state["terminals"].items() if t["worktree"] == given["--worktree"] and alive(t["pid"])]
            )
        )
    elif key == ("terminal", "wait"):
        pid = state["terminals"][given["--terminal"]]["pid"]
        deadline = time.monotonic() + int(given["--timeout-ms"]) / 1000
        while alive(pid):
            if time.monotonic() > deadline:
                print("timeout", file=sys.stderr)
                return 1
            time.sleep(0.05)
    elif key not in (("repo", "set"), ("worktree", "rm")):
        print(f"unknown command {args}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
