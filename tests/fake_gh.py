"""Fake `gh` CLI that stands in for GitHub with issues, sub-issues, blocked-by links and pull requests.

It takes its state directory first and the `gh` arguments after it.
`state.json` there holds the issues of each repository, the bare git repository that stands for every remote,
the pull requests opened so far, whether every call fails as `gh` without access does
and whether `pr create` fails, and `calls.jsonl` records every call.
`api --paginate` prints two items per page with the pages back to back, as `gh` without `--slurp` does.
`api` also takes comments (`-f body=`) and labels (`-f labels[]=`, `-X DELETE .../labels/<name>`) on an issue,
which it keeps on the issue as `comments` and `labels`.
`api --paginate repos/<repo>/issues/<n>/comments` lists the comments of an issue, or of the pull request of that number,
which the fake keeps as the text of one posted by `user` or as `{"author", "body"}`, with their place in the list as id.
`api --paginate repos/<repo>/pulls/<n>/comments` lists the `threads` of the pull request, each with its `id`, `path`,
`line` and `in_reply_to`, and `.../reviews` its `reviews`, each with its `id`, `body` and `state`.
Each of these prints its `at` as its time, and `association` as its `author_association`, `MEMBER` when it has none.
`api --paginate repos/<repo>/issues/<n>/events` lists the `events` of an issue or a pull request, each with its `id`,
`event`, `actor`, `label` and `at`.
`pr create` refuses a head branch the bare repository does not hold, as GitHub does.
`pr view <url> --json headRefOid` prints the commit the PR's head branch is at in the bare repository,
unless `head_oid` in the state names another one, as GitHub does for a push it has not taken in yet.
`pr view <url> --json state` prints `MERGED` for a pull request `pr merge` merged, and fails while `state_fails` counts
down.
`pr merge <url> --<method> --match-head-commit <sha>` keeps the method as `merged` of the pull request, and fails with
`merge_refused` when that is set or with another head, as GitHub does; with `merge_queued` it takes the merge and
merges nothing.
`pr checks <url> --json name,bucket,link` prints the checks of that commit from `checks`, which maps a commit,
or `*` for the commit the PR's branch is at, to a list of snapshots; each call takes the next snapshot
and the last one stays. A commit without a snapshot has no checks, and `gh` then fails with `no checks reported`.
`run view --job <id> --log-failed` prints `logs[<id>]`.
`api repos/<repo>/issues?state=open&labels=a,b` lists the open issues that carry every label, as `gh` does without
`--slurp`, and `api repos/<repo>/milestones` lists the open milestones of `milestones`.
`api repos/<repo>/issues -f title= -f body= -f labels[]= -F milestone=<number>` files an issue with the next number,
unless `create_fails` counts down a failure, which `gh` prints as a server error.
`api user` prints `user`, the account `gh` is logged in as, and an issue prints its `assignees`.
`api repos/<repo>/issues/<n>/assignees -f assignees[]=` adds assignees and prints the issue,
and with `assign_ignored` it prints the issue without them and no error, as GitHub does for a user without rights.
`pulls/<n>/comments` and `.../reviews` fail while `comments_fail` counts down, once `comments_pass` has counted down.
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
from urllib.parse import parse_qs, unquote

INITIAL: dict[str, Any] = {
    "repos": {},
    "remote": "",
    "pulls": [],
    "denied": False,
    "pr_fails": False,
    "head_oid": "",
    "checks": {},
    "logs": {},
    "create_fails": 0,
    "user": {"login": "factory-bot"},
    "assign_ignored": False,
    "merge_refused": "",
    "merge_queued": False,
    "state_fails": 0,
    "comments_pass": 0,
    "comments_fail": 0,
}

COLLECTION = re.compile(r"repos/([^/]+/[^/]+)/(issues|milestones)")
ISSUE = re.compile(
    r"repos/([^/]+/[^/]+)/issues/(\d+)(/sub_issues|/dependencies/blocked_by|/comments|/events|/assignees|/labels(?:/.+)?)?"
)
PULL = re.compile(r"repos/([^/]+/[^/]+)/pulls/(\d+)/(comments|reviews)")
PAGE = 2
# The time of what a test wrote without one, which comes before every time a test gives.
LONG_AGO = "2000-01-01T00:00:00Z"


@contextlib.contextmanager
def state(root: Path) -> Iterator[dict[str, Any]]:
    """Locks the state for one read-modify-write."""
    with (root / "state.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root / "state.json"
        data = json.loads(path.read_text()) if path.exists() else json.loads(json.dumps(INITIAL))
        try:
            yield data
        finally:
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
        "assignees": [{"login": login} for login in issue.get("assignees", [])],
        "labels": [{"name": name} for name in issue.get("labels", [])],
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
    if args == ["user"]:
        print(json.dumps(data["user"]))
        return
    paginate = "--paginate" in args
    [path] = [arg for arg in args if arg.startswith("repos/")]
    collection = COLLECTION.fullmatch(path.partition("?")[0])
    if collection is not None:
        serve_collection(data, collection.group(1), collection.group(2), path, args)
        return
    pull = PULL.fullmatch(path.partition("?")[0])
    if pull is not None:
        if data["comments_pass"]:
            data["comments_pass"] -= 1
        elif data["comments_fail"]:
            data["comments_fail"] -= 1
            fail("gh: Server Error (HTTP 502)")
        print_pages(discussion(data, int(pull.group(2)), pull.group(3)), args)
        return
    match = ISSUE.fullmatch(path.partition("?")[0])
    if match is None:
        fail(f"gh: unknown path {path}")
    assert match is not None
    repo, number, tail = match.groups()
    issue = data["repos"].get(repo, {}).get("issues", {}).get(number)
    if tail == "/comments" and paginate:
        talked = issue if issue is not None else data["pulls"][int(number) - 1]
        print_pages(conversation(data, talked), args)
        return
    if tail == "/events":
        talked = issue if issue is not None else data["pulls"][int(number) - 1]
        print_pages(events_of(talked), args)
        return
    if issue is None:
        fail("gh: Not Found (HTTP 404)")
    if tail is None:
        print(json.dumps(as_json(repo, number, issue)))
        return
    if tail == "/assignees":
        if not data["assign_ignored"]:
            added = [
                value.removeprefix("assignees[]=") for flag, value in zip(args, args[1:], strict=False) if flag == "-f"
            ]
            issue.setdefault("assignees", []).extend(added)
        print(json.dumps(as_json(repo, number, issue)))
        return
    if tail.startswith(("/comments", "/labels")):
        write_issue(issue, tail, args)
        return
    print_pages(linked(data, repo, issue["sub_issues" if tail == "/sub_issues" else "blocked_by"]), args)


def print_pages(items: list[dict[str, Any]], args: list[str]) -> None:
    pages = [items[i : i + PAGE] for i in range(0, len(items), PAGE)] or [[]]
    print("".join(json.dumps(page) for page in (pages if "--paginate" in args else pages[:1])))


def conversation(data: dict[str, Any], talked: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns the comments of an issue or a pull request, as GitHub prints them."""
    found = []
    for at, comment in enumerate(talked.get("comments", []), 1):
        said = comment if isinstance(comment, dict) else {"author": data["user"]["login"], "body": comment}
        found.append({"id": at, "user": {"login": said["author"]}, "body": said["body"], **written(said)})
    return found


def written(said: dict[str, Any]) -> dict[str, str]:
    """Returns when GitHub says something was written and how its author relates to the repository."""
    return {"created_at": said.get("at", LONG_AGO), "author_association": said.get("association", "MEMBER")}


def events_of(talked: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns the events of an issue or a pull request, as GitHub prints them."""
    return [
        {
            "id": event["id"],
            "event": event["event"],
            "actor": {"login": event["actor"]},
            "label": {"name": event["label"]},
            "created_at": event["at"],
        }
        for event in talked.get("events", [])
    ]


def discussion(data: dict[str, Any], number: int, kind: str) -> list[dict[str, Any]]:
    """Returns the review comments or the reviews of a pull request, as GitHub prints them."""
    pull = data["pulls"][number - 1]
    if kind == "reviews":
        return [
            {
                **review,
                "user": {"login": review["author"]},
                "submitted_at": review.get("at", LONG_AGO),
                "author_association": review.get("association", "MEMBER"),
            }
            for review in pull.get("reviews", [])
        ]
    return [
        {
            "id": thread["id"],
            "user": {"login": thread["author"]},
            "body": thread["body"],
            "path": thread["path"],
            "line": thread["line"],
            **written(thread),
            **({"in_reply_to_id": thread["in_reply_to"]} if "in_reply_to" in thread else {}),
        }
        for thread in pull.get("threads", [])
    ]


def serve_collection(data: dict[str, Any], repo: str, kind: str, path: str, args: list[str]) -> None:
    """Lists the open issues or milestones of the repository, or files an issue when the call carries fields."""
    found = data["repos"].setdefault(repo, {"issues": {}})
    fields = [value for flag, value in zip(args, args[1:], strict=False) if flag in ("-f", "-F")]
    if kind == "milestones":
        items = [{"number": m["number"], "title": m["title"]} for m in found.get("milestones", [])]
    elif fields:
        file_issue(data, repo, found, fields)
        return
    else:
        wanted = [name for name in parse_qs(path.partition("?")[2]).get("labels", [""])[0].split(",") if name]
        items = [
            as_json(repo, number, issue)
            for number, issue in found["issues"].items()
            if issue["state"] == "open" and set(wanted) <= set(issue.get("labels", []))
        ]
    print_pages(items, args)


def file_issue(data: dict[str, Any], repo: str, found: dict[str, Any], fields: list[str]) -> None:
    """Files an issue from the fields of `api -f`, the way GitHub does, and prints it."""
    if data["create_fails"] > 0:
        data["create_fails"] -= 1
        fail("gh: Server Error (HTTP 502)")
    values = [field.partition("=")[::2] for field in fields]
    number = max((int(n) for n in found["issues"]), default=0) + 1
    milestone = next((value for key, value in values if key == "milestone"), None)
    found["issues"][str(number)] = {
        "title": next(value for key, value in values if key == "title"),
        "body": next(value for key, value in values if key == "body"),
        "state": "open",
        "labels": [value for key, value in values if key == "labels[]"],
        "milestone": int(milestone) if milestone else None,
        "sub_issues": [],
        "blocked_by": [],
    }
    print(json.dumps(as_json(repo, str(number), found["issues"][str(number)])))


def write_issue(issue: dict[str, Any], tail: str, args: list[str]) -> None:
    """Applies a comment, a label or a label removal to the issue, as GitHub does."""
    fields = [value for flag, value in zip(args, args[1:], strict=False) if flag == "-f"]
    if tail == "/comments":
        issue.setdefault("comments", []).append(fields[0].removeprefix("body="))
        return
    labels = issue.setdefault("labels", [])
    if tail == "/labels":
        added = [field.removeprefix("labels[]=") for field in fields]
        labels.extend(name for name in added if name not in labels)
        return
    name = unquote(tail.removeprefix("/labels/"))
    if name not in labels:
        fail("gh: Label does not exist (HTTP 404)")
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


def head_of(data: dict[str, Any], url: str) -> str:
    """Returns the commit the head branch of the PR at `url` is at."""
    if data["head_oid"]:
        return str(data["head_oid"])
    pull = data["pulls"][int(url.rsplit("/", 1)[1]) - 1]
    found = subprocess.run(
        ["git", "--git-dir", data["remote"], "rev-parse", f"refs/heads/{pull['head']}"], capture_output=True, text=True
    )
    return found.stdout.strip()


def pr_checks(data: dict[str, Any], url: str) -> None:
    snapshots = data["checks"].get(head_of(data, url)) or data["checks"].get("*")
    if not snapshots:
        fail("no checks reported on the 'factory' branch")
    assert snapshots is not None
    print(json.dumps(snapshots[0]))
    if len(snapshots) > 1:
        del snapshots[0]


def pr_view(data: dict[str, Any], url: str, fields: str) -> None:
    if fields == "state":
        if data["state_fails"]:
            data["state_fails"] -= 1
            fail("HTTP 502: Bad Gateway (https://api.github.com/graphql)")
        merged = data["pulls"][int(url.rsplit("/", 1)[1]) - 1].get("merged")
        print(json.dumps({"state": "MERGED" if merged else "OPEN"}))
    else:
        print(json.dumps({"headRefOid": head_of(data, url)}))


def pr_merge(data: dict[str, Any], url: str, method: str, commit: str) -> None:
    if data["merge_refused"]:
        fail(data["merge_refused"])
    if commit != head_of(data, url):
        fail("GraphQL: Head branch was modified. Review and try the merge again. (mergePullRequest)")
    if not data["merge_queued"]:
        data["pulls"][int(url.rsplit("/", 1)[1]) - 1]["merged"] = method.removeprefix("--")


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
        elif command == "pr" and args[0] == "edit":
            flags = dict(zip(args[2::2], args[3::2], strict=True))
            data["pulls"][int(args[1].rsplit("/", 1)[1]) - 1]["body"] = Path(flags["--body-file"]).read_text()
        elif command == "pr" and args[0] == "view":
            pr_view(data, args[1], args[args.index("--json") + 1])
        elif command == "pr" and args[0] == "merge":
            pr_merge(data, args[1], args[2], args[args.index("--match-head-commit") + 1])
        elif command == "pr" and args[0] == "checks":
            pr_checks(data, args[1])
        elif command == "run" and args[0] == "view":
            print(data["logs"][args[args.index("--job") + 1]])
        else:
            fail(f"unknown command {command}")


if __name__ == "__main__":
    main()
