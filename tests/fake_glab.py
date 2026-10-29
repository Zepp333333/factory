"""Fake `glab` CLI that stands in for GitLab with issues, child Tasks, blocked-by links and merge requests.

It takes its state directory first and the `glab` arguments after it.
`state.json` there holds the projects of each host, whether every call fails as `glab` without access does,
and whether the instance has the "is blocked by" links of Premium (`linked_items`),
and `signed_out` and `unreadable` list the hosts without a sign-in and the projects without access,
and `calls.jsonl` records every call.
It also takes notes (`-X POST projects/<path>/issues/<iid>/notes -f body=`) and label changes
(`-X PUT projects/<path>/issues/<iid> -f add_labels=` or `-f remove_labels=`) on an issue,
which it keeps on the work item as `notes` and `labels`; a label already there or gone is no error, as in GitLab.
`api projects/<path>/merge_requests/<n>` prints the MR with its `sha`, the commit of its source branch in the bare
repository unless `mr_head` names another one, and its `head_pipeline`, which `pipelines` holds.
`pipelines` maps a commit, or `*` for the commit the branch is at, to a list of snapshots, each call to the MR
takes the next one and the last stays; a snapshot is `{"status": ..., "jobs": [{"id", "name", "web_url", "trace"}]}`
with the failed jobs, and a commit without one has no head pipeline.
The fake serves `.../pipelines/<id>/jobs?scope[]=failed` and `.../jobs/<id>/trace` from the snapshot.
It answers `api graphql --hostname <host> -f query=...` the way GitLab's work item GraphQL API does:
a work item has the widgets `DESCRIPTION`, `HIERARCHY` and, on Premium and up, `LINKED_ITEMS`,
a state of `OPEN` or `CLOSED`, and an unknown project comes back as `{"data": {"project": null}}`.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import unquote

INITIAL: dict[str, Any] = {
    "hosts": {},
    "denied": False,
    "linked_items": False,
    "children_have_next_page": False,
    "remote": "",
    "default_branch": "main",
    "merge_requests": [],
    "mr_fails": False,
    "mr_head": "",
    "pipelines": {},
    "signed_out": [],
    "unreadable": [],
}

PROJECT = re.compile(r'project\(fullPath: ("(?:[^"\\]|\\.)*")\)')
IID = re.compile(r'workItems\(iid: ("(?:[^"\\]|\\.)*")\)')
ISSUE = re.compile(r"projects/([^/]+)/issues/(\d+)(/notes)?")
MERGE_REQUEST = re.compile(r"projects/[^/]+/merge_requests/(\d+)")
JOBS = re.compile(r"projects/[^/]+/pipelines/(\d+)/jobs\?.*")
TRACE = re.compile(r"projects/[^/]+/jobs/(\d+)/trace")


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(1)


def item(data: dict[str, Any], project: str, iid: str, deep: bool) -> dict[str, Any]:
    """Returns the work item as GitLab's GraphQL API prints it; a child prints a bare `HIERARCHY` widget."""
    issues = data["hosts"][data["host"]]["projects"][project]["items"]
    found = issues[iid]
    widgets: list[dict[str, Any]] = [{"type": "DESCRIPTION", "description": found["description"]}]
    if deep:
        children = [item(data, project, ref, False) for ref in found.get("children", [])]
        widgets.append(
            {
                "type": "HIERARCHY",
                "children": {"pageInfo": {"hasNextPage": data["children_have_next_page"]}, "nodes": children},
            }
        )
    else:
        widgets.append({"type": "HIERARCHY"})
    if data["linked_items"]:
        links = [linked(data, project, ref) for ref in found.get("blocked_by", [])]
        widgets.append({"type": "LINKED_ITEMS", "linkedItems": {"nodes": links}})
    return {
        "iid": iid,
        "title": found["title"],
        "state": found["state"].upper(),
        "workItemType": {"name": found.get("type", "Task")},
        "namespace": {"fullPath": project},
        "widgets": widgets,
    }


def linked(data: dict[str, Any], project: str, ref: str) -> dict[str, Any]:
    """Returns the link to the blocker `ref`, which may name an issue of another project as `group/project#12`."""
    owner, _, iid = ref.rpartition("#") if "#" in ref else (project, "", ref)
    found = data["hosts"][data["host"]]["projects"][owner]["items"][iid]
    node = {"iid": iid, "state": found["state"].upper(), "namespace": {"fullPath": owner}}
    return {"linkType": "is_blocked_by", "workItem": node}


def graphql(data: dict[str, Any], query: str) -> None:
    project = json.loads(PROJECT.search(query).group(1))  # type: ignore[union-attr]
    iid = json.loads(IID.search(query).group(1))  # type: ignore[union-attr]
    projects = data["hosts"].get(data["host"], {}).get("projects", {})
    if project not in projects:
        print(json.dumps({"data": {"project": None}}))
        return
    nodes = [item(data, project, iid, "children(first" in query)] if iid in projects[project]["items"] else []
    print(json.dumps({"data": {"project": {"workItems": {"nodes": nodes}}}}))


def mr_create(data: dict[str, Any], args: list[str]) -> None:
    """Opens a merge request, which GitLab refuses for a source branch the project does not hold."""
    flags = dict(zip(args[::2], args[1::2], strict=False))
    head = flags["--source-branch"]
    found = subprocess.run(
        ["git", "--git-dir", data["remote"], "rev-parse", "--verify", "--quiet", f"refs/heads/{head}"],
        capture_output=True,
    )
    if data["mr_fails"] or found.returncode != 0:
        fail("POST https://gitlab.example.com/api/v4/projects/merge_requests: 400 {message: source branch is invalid}")
    data["merge_requests"].append(
        {
            "repo": flags["--repo"],
            "base": flags["--target-branch"],
            "head": head,
            "title": flags["--title"],
            "body": flags["--description"],
        }
    )
    print(f"Creating merge request for {head} into {flags['--target-branch']} in {flags['--repo']}\n")
    print(f"https://{flags['--repo']}/-/merge_requests/{len(data['merge_requests'])}")


def merge_request(data: dict[str, Any], path: str) -> None:
    """Prints the MR as GitLab's REST API does, with the head pipeline of the commit its branch is at."""
    number = int(MERGE_REQUEST.fullmatch(path).group(1))  # type: ignore[union-attr]
    head = data["merge_requests"][number - 1]["head"]
    found = subprocess.run(
        ["git", "--git-dir", data["remote"], "rev-parse", f"refs/heads/{head}"], capture_output=True, text=True
    )
    sha = data["mr_head"] or found.stdout.strip()
    snapshots = data["pipelines"].get(sha) or data["pipelines"].get("*")
    pipeline = None
    if snapshots:
        pipeline = {"id": 7, "sha": sha, "status": snapshots[0]["status"]}
        data["jobs"] = snapshots[0].get("jobs", [])
        if len(snapshots) > 1:
            del snapshots[0]
    print(json.dumps({"sha": sha, "head_pipeline": pipeline}))


def write_issue(data: dict[str, Any], path: str, args: list[str]) -> None:
    """Applies a note or a label change to the issue, as GitLab's REST API does."""
    match = ISSUE.fullmatch(path)
    if match is None:
        fail(f"glab: unknown path {path}")
    project, iid, notes = unquote(match.group(1)), match.group(2), match.group(3)
    projects = data["hosts"].get(data["host"], {}).get("projects", {})
    found = projects.get(project, {}).get("items", {}).get(iid)
    if found is None:
        fail("glab: 404 Not Found")
    fields = dict(value.split("=", 1) for flag, value in zip(args, args[1:], strict=False) if flag == "-f")
    if notes:
        found.setdefault("notes", []).append(fields["body"])
        return
    labels = found.setdefault("labels", [])
    labels.extend(name for name in fields.get("add_labels", "").split(",") if name and name not in labels)
    for name in fields.get("remove_labels", "").split(","):
        if name in labels:
            labels.remove(name)


def main() -> None:
    root = Path(sys.argv[1])
    command, *args = sys.argv[2:]
    with (root / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps([command, *args]) + "\n")
    data = json.loads((root / "state.json").read_text())
    if data["denied"]:
        fail("glab: 401 Unauthorized")
    if command == "mr" and args[0] == "create":
        mr_create(data, args[1:])
        (root / "state.json").write_text(json.dumps(data))
        return
    if command == "auth" and args[:2] == ["status", "--hostname"]:
        if args[2] in data["signed_out"] or args[2] not in data["hosts"]:
            fail(f"x No token found for {args[2]}")
        return
    if (
        command == "api"
        and args[0].startswith("projects/")
        and not any(pattern.match(args[0]) for pattern in (ISSUE, MERGE_REQUEST, JOBS, TRACE))
        and args[1:2] == ["--hostname"]
    ):
        project = unquote(args[0].removeprefix("projects/"))
        projects = data["hosts"].get(args[2], {}).get("projects", {})
        if project not in projects or project in data["unreadable"]:
            fail("glab: 404 Project Not Found")
        print(json.dumps({"path_with_namespace": project, "default_branch": data["default_branch"]}))
        return
    if command != "api" or "--hostname" not in args:
        fail(f"unknown command {command} {' '.join(args)}")
    host = args[args.index("--hostname") + 1]
    data["host"] = host
    if host not in data["hosts"]:
        api = "graphql" if args[0] == "graphql" else "v4"
        fail(f'Get "https://{host}/api/{api}": dial tcp: lookup {host}: no such host')
    if args[0] == "graphql":
        graphql(data, args[args.index("-f") + 1].removeprefix("query="))
        return
    [path] = [arg for arg in args if arg.startswith("projects/")]
    if MERGE_REQUEST.fullmatch(path):
        merge_request(data, path)
    elif JOBS.fullmatch(path):
        print(json.dumps([{k: v for k, v in job.items() if k != "trace"} for job in data.get("jobs", [])]))
    elif match := TRACE.fullmatch(path):
        print(next(job["trace"] for job in data.get("jobs", []) if str(job["id"]) == match.group(1)))
    else:
        write_issue(data, path, args)
    (root / "state.json").write_text(json.dumps(data))


if __name__ == "__main__":
    main()
