"""Fake `gh` CLI that stands in for GitHub with issues, sub-issues, blocked-by links and pull requests.

It takes its state directory first and the `gh` arguments after it.
`state.json` there holds the issues of each repository, the bare git repository that stands for every remote,
the pull requests opened so far, whether every call fails as `gh` without access does
and whether `pr create` fails, and `calls.jsonl` records every call.
`api --paginate` prints two items per page with the pages back to back, as `gh` without `--slurp` does.
`api` also takes comments (`-f body=`) and labels (`-f labels[]=`, `-X DELETE .../labels/<name>`) on an issue,
which it keeps on the issue as `comments` and `labels`.
`pr create` refuses a head branch the bare repository does not hold, as GitHub does.
"""

import contextlib
import fcntl
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote

INITIAL: dict[str, Any] = {"repos": {}, "remote": "", "pulls": [], "denied": False, "pr_fails": False}

ISSUE = re.compile(r"repos/([^/]+/[^/]+)/issues/(\d+)(/sub_issues|/dependencies/blocked_by|/comments|/labels(?:/.+)?)?")
PAGE = 2


@contextlib.contextmanager
def state(root: Path) -> Iterator[dict[str, Any]]:
    """Locks the state for one read-modify-write."""
    with (root / "state.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "state.json"
        data = json.loads(path.read_text()) if path.exists() else json.loads(json.dumps(INITIAL))
        yield data
        path.write_text(json.dumps(data))


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def as_json(repo: str, number: str, issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": int(number),
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "repository_url": f"https://api.github.com/repos/{repo}",
    }


def linked(data: dict[str, Any], repo: str, refs: list[str]) -> list[dict[str, Any]]:
    """Returns the issues behind refs such as `12` in `repo` or `other/repo#12`."""
    found = []
    for ref in refs:
        owner, _, number = ref.rpartition("#") if "#" in ref else (repo, "", ref)
        found.append(as_json(owner, number, data["repos"][owner]["issues"][number]))
    return found


def api(data: dict[str, Any], args: list[str]) -> None:
    paginate = "--paginate" in args
    [path] = [arg for arg in args if arg.startswith("repos/")]
    match = ISSUE.fullmatch(path.partition("?")[0])
    if match is None:
        fail(f"gh: unknown path {path}")
    assert match is not None
    repo, number, tail = match.groups()
    issue = data["repos"].get(repo, {}).get("issues", {}).get(number)
    if issue is None:
        fail("gh: Not Found (HTTP 404)")
    if tail is None:
        print(json.dumps(as_json(repo, number, issue)))
        return
    if tail is not None and tail.startswith(("/comments", "/labels")):
        write_issue(issue, tail, args)
        return
    items = linked(data, repo, issue["sub_issues" if tail == "/sub_issues" else "blocked_by"])
    pages = [items[i : i + PAGE] for i in range(0, len(items), PAGE)] or [[]]
    print("".join(json.dumps(page) for page in (pages if paginate else pages[:1])))


def write_issue(issue: dict[str, Any], tail: str, args: list[str]) -> None:
    """Applies a comment or label call to the issue, as GitHub does."""
    fields = [value for flag, value in zip(args, args[1:], strict=False) if flag == "-f"]
    labels = issue.setdefault("labels", [])
    if tail == "/comments":
        issue.setdefault("comments", []).append(fields[0].removeprefix("body="))
    elif tail == "/labels":
        labels.extend(f.removeprefix("labels[]=") for f in fields if f.removeprefix("labels[]=") not in labels)
    else:
        name = unquote(tail.removeprefix("/labels/"))
        if name not in labels:
            fail("gh: Not Found (HTTP 404)")
        labels.remove(name)


def pr_create(data: dict[str, Any], args: list[str]) -> None:
    flags = dict(zip(args[::2], args[1::2], strict=True))
    head = flags["--head"]
    found = subprocess.run(
        ["git", "--git-dir", data["remote"], "rev-parse", "--verify", "--quiet", f"refs/heads/{head}"],
        capture_output=True,
    )
    if data["pr_fails"] or found.returncode != 0:
        fail("pull request create failed: GraphQL: Head sha can't be blank (createPullRequest)")
    data["pulls"].append(
        {
            "repo": flags["--repo"],
            "base": flags["--base"],
            "head": head,
            "title": flags["--title"],
            "body": Path(flags["--body-file"]).read_text(),
        }
    )
    print(f"https://github.com/{flags['--repo']}/pull/{len(data['pulls'])}")


def main() -> None:
    root = Path(sys.argv[1])
    command, *args = sys.argv[2:]
    with (root / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps([command, *args]) + "\n")
    with state(root) as data:
        if data["denied"]:
            fail("HTTP 401: Bad credentials (https://api.github.com/graphql)")
        if command == "api":
            api(data, args)
        elif command == "pr" and args[0] == "create":
            pr_create(data, args[1:])
        else:
            fail(f"unknown command {command}")


if __name__ == "__main__":
    main()
