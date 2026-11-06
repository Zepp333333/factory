"""Fake `glab` CLI that stands in for GitLab with issues, child Tasks, blocked-by links and merge requests.

It takes its state directory first and the `glab` arguments after it.
`state.json` there holds the projects of each host, whether every call fails as `glab` without access does,
and whether the instance has the "is blocked by" links of Premium (`linked_items`),
and `signed_out` and `unreadable` list the hosts without a sign-in and the projects without access,
and `calls.jsonl` records every call.
It also takes notes (`-X POST projects/<path>/issues/<iid>/notes -f body=`) and label changes
(`-X PUT projects/<path>/issues/<iid> -f add_labels=` or `-f remove_labels=`) on an issue,
which it keeps on the work item as `notes` and `labels`; a label already there or gone is no error, as in GitLab.
`api --paginate projects/<path>/issues/<iid>/discussions` prints each note of an issue as a discussion of its own,
where a note is the text of one posted by `user` or `{"author", "body", "system"}`, and
`.../merge_requests/<n>/discussions` prints the `discussions` of a merge request, each a page of its own.
`.../merge_requests/<n>/resource_label_events` prints the `label_events` of a merge request, or of an issue under
`.../issues/<iid>/...`, and `projects/<path>/members/all` prints `members`, usernames by their access level.
`api projects/<path>/issues?search=<text>&in=description&per_page=<n>&page=<k>` lists the issues of the project whose
description holds the text, as GitLab's REST API does, `search_page_size` of them at most on a page, newest first.
`mr merge <url> --repo <repo> --yes [--squash | --rebase] [--sha <commit>]` keeps the method as `merged` of the MR, and
fails with `mr_merge_refused` when that is set or with another head, as GitLab does.
`api projects/<path>/merge_requests/<n>` prints the MR with its `state` and its `sha`, the commit of its source branch
in the bare repository unless `mr_head` names another one, and its `head_pipeline`, which `pipelines` holds.
`pipelines` maps a commit, or `*` for the commit the branch is at, to a list of snapshots, each call to the MR
takes the next one and the last stays; a snapshot is `{"status": ..., "jobs": [{"id", "name", "web_url", "trace"}]}`
with the failed jobs, and a commit without one has no head pipeline.
`api projects/<path>/issues?state=opened&labels=a,b` lists the open issues that carry every label,
`api projects/<path>/milestones?title=` lists the active milestones of `milestones` with that title,
and `api -X POST projects/<path>/issues -f title= -f description= -f labels=a,b -f milestone_id=` files an issue with
the next iid, unless `create_fails` counts down a failure.
The fake serves `.../pipelines/<id>/jobs?scope[]=failed` and `.../jobs/<id>/trace` from the snapshot.
It answers `api graphql --hostname <host> -f query=...` the way GitLab's work item GraphQL API does:
a work item has the widgets `DESCRIPTION`, `HIERARCHY` and, on Premium and up, `LINKED_ITEMS`,
a state of `OPEN` or `CLOSED`, and an unknown project comes back as `{"data": {"project": null}}`.
`api --hostname <host> user` prints `user`, the owner of the token, `api projects/<path>/issues/<iid>` prints the issue
with its `assignees`, which a work item keeps as usernames of `USERS`, and
`api -X PUT projects/<path>/issues/<iid>?assignee_ids[]=<id>` replaces them and prints the issue,
and with `assign_ignored` it prints the issue as it was and no error, as GitLab does for a user without rights.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import parse_qs, unquote

INITIAL: dict[str, Any] = {
    "hosts": {},
    "denied": False,
    "linked_items": False,
    "children_have_next_page": False,
    "search_page_size": 100,
    "remote": "",
    "default_branch": "main",
    "merge_requests": [],
    "mr_fails": False,
    "mr_head": "",
    "mr_merge_refused": "",
    "pipelines": {},
    "signed_out": [],
    "unreadable": [],
    "repo_labels": {},
    "create_fails": 0,
    "user": {"id": 7, "username": "factory-bot"},
    "assign_ignored": False,
}

USERS = {"factory-bot": 7, "mona": 8}
# The time of what a test wrote without one, which comes before every time a test gives.
LONG_AGO = "2000-01-01T00:00:00+00:00"

PROJECT = re.compile(r'project\(fullPath: ("(?:[^"\\]|\\.)*")\)')
IID = re.compile(r'workItems\(iid: ("(?:[^"\\]|\\.)*")\)')
COLLECTION = re.compile(r"projects/([^/]+)/(issues|milestones)(?:\?.*)?")
SEARCH = re.compile(r"projects/([^/]+)/issues\?((?:.*&)?search=.*)")
ISSUE = re.compile(r"projects/([^/]+)/issues/(\d+)(/notes)?")
MERGE_REQUEST = re.compile(r"projects/[^/]+/merge_requests/(\d+)")
DISCUSSIONS = re.compile(r"projects/([^/]+)/(issues|merge_requests)/(\d+)/discussions(?:\?.*)?")
LABEL_EVENTS = re.compile(r"projects/([^/]+)/(issues|merge_requests)/(\d+)/resource_label_events(?:\?.*)?")
MEMBERS = re.compile(r"projects/[^/]+/members/all(?:\?.*)?")
JOBS = re.compile(r"projects/[^/]+/pipelines/(\d+)/jobs\?.*")
TRACE = re.compile(r"projects/[^/]+/jobs/(\d+)/trace")


def fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(1)


def item(data: dict[str, Any], project: str, iid: str, deep: bool) -> dict[str, Any]:
    """Returns the work item as GitLab's GraphQL API prints it; a child prints a bare `HIERARCHY` widget."""
    issues = data["hosts"][data["host"]]["projects"][project]["items"]
    found = issues[iid]
    widgets: list[dict[str, Any]] = [
        {"type": "DESCRIPTION", "description": found["description"]},
        {"type": "LABELS", "labels": {"nodes": [{"title": name} for name in found.get("labels", [])]}},
    ]
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
    state = "merged" if data["merge_requests"][number - 1].get("merged") else "opened"
    print(json.dumps({"state": state, "sha": sha, "head_pipeline": pipeline}))


def mr_merge(data: dict[str, Any], url: str, args: list[str]) -> None:
    """Merges the MR by the flags of `glab mr merge`, which GitLab refuses at a `--sha` its branch is not at."""
    found = data["merge_requests"][int(url.rsplit("/", 1)[1]) - 1]
    if data["mr_merge_refused"]:
        fail(data["mr_merge_refused"])
    head = subprocess.run(
        ["git", "--git-dir", data["remote"], "rev-parse", f"refs/heads/{found['head']}"], capture_output=True, text=True
    ).stdout.strip()
    if "--sha" in args and args[args.index("--sha") + 1] != head:
        fail("PUT merge_requests/merge: 409 {message: SHA does not match HEAD of source branch}")
    found["merged"] = "squash" if "--squash" in args else "rebase" if "--rebase" in args else "merge"


def search(data: dict[str, Any], path: str) -> None:
    """Prints one page of the issues whose description holds the search text, as GitLab's REST API does."""
    match = SEARCH.fullmatch(path)
    assert match is not None
    project = unquote(match.group(1))
    query = parse_qs(match.group(2))
    items = data["hosts"][data["host"]]["projects"].get(project, {}).get("items", {})
    found = [
        {
            "id": 1000 + int(iid),
            "iid": int(iid),
            "project_id": 1,
            "title": found["title"],
            "description": found["description"],
            "state": "opened" if found["state"] == "open" else found["state"],
            "labels": found.get("labels", []),
            "issue_type": found.get("type", "Task").lower(),
            "web_url": f"https://{data['host']}/{project}/-/issues/{iid}",
        }
        for iid, found in sorted(items.items(), key=lambda pair: -int(pair[0]))
        if query["search"][0] in found["description"]
    ]
    size = min(int(query["per_page"][0]), data["search_page_size"])
    start = (int(query["page"][0]) - 1) * size
    print(json.dumps(found[start : start + size]))


def write_issue(data: dict[str, Any], path: str, args: list[str]) -> None:
    """Reads the issue, or applies a note, a label change or a change of assignees to it, as GitLab's REST API does."""
    path, _, query = path.partition("?")
    match = ISSUE.fullmatch(path)
    if match is None:
        fail(f"glab: unknown path {path}")
    project, iid, notes = unquote(match.group(1)), match.group(2), match.group(3)
    projects = data["hosts"].get(data["host"], {}).get("projects", {})
    found = projects.get(project, {}).get("items", {}).get(iid)
    if found is None:
        fail("glab: 404 Not Found")
    if query and not data["assign_ignored"]:
        names = {id: name for name, id in USERS.items()}
        found["assignees"] = [names[int(id)] for id in parse_qs(query)["assignee_ids[]"]]
    if query or "-X" not in args:
        assignees = [{"id": USERS[name], "username": name} for name in found.get("assignees", [])]
        print(json.dumps({"iid": int(iid), "assignees": assignees}))
        return
    fields = dict(value.split("=", 1) for flag, value in zip(args, args[1:], strict=False) if flag == "-f")
    if notes:
        found.setdefault("notes", []).append(fields["body"])
        return
    labels = found.setdefault("labels", [])
    labels.extend(name for name in fields.get("add_labels", "").split(",") if name and name not in labels)
    for name in fields.get("remove_labels", "").split(","):
        if name in labels:
            labels.remove(name)


def discussions(data: dict[str, Any], path: str) -> None:
    """Prints the discussions of an issue or a merge request as GitLab does, with a page per discussion."""
    match = DISCUSSIONS.fullmatch(path)
    assert match is not None
    project, kind, iid = unquote(match.group(1)), match.group(2), match.group(3)
    if kind == "merge_requests":
        found = data["merge_requests"][int(iid) - 1].get("discussions", [])
    else:
        notes = data["hosts"][data["host"]]["projects"][project]["items"][iid].get("notes", [])
        login = data["user"]["username"]
        said = [note if isinstance(note, dict) else {"author": login, "body": note} for note in notes]
        found = [{"id": f"d{at}", "individual_note": True, "notes": [note]} for at, note in enumerate(said, 1)]
    next_note = 1
    for item in found:
        shown = []
        for note in item["notes"]:
            shown.append(
                {"created_at": LONG_AGO, **note, "id": next_note, "author": {"username": note["author"]}},
            )
            next_note += 1
        print(json.dumps([{**item, "notes": shown}]))
    if not found:
        print("[]")


def label_events(data: dict[str, Any], path: str) -> None:
    """Prints the label changes of an issue or a merge request as GitLab does."""
    match = LABEL_EVENTS.fullmatch(path)
    assert match is not None
    project, kind, iid = unquote(match.group(1)), match.group(2), match.group(3)
    if kind == "merge_requests":
        found = data["merge_requests"][int(iid) - 1]
    else:
        found = data["hosts"][data["host"]]["projects"][project]["items"][iid]
    print(json.dumps(found.get("label_events", [])))


def serve_collection(root: Path, data: dict[str, Any], path: str, args: list[str]) -> None:
    """Lists the open issues or the active milestones of the project, or files an issue on a POST."""
    match = COLLECTION.fullmatch(path)
    assert match is not None
    project, kind = unquote(match.group(1)), match.group(2)
    found = data["hosts"][data["host"]]["projects"].setdefault(project, {"items": {}})
    query = parse_qs(path.partition("?")[2])
    fields = dict(value.split("=", 1) for flag, value in zip(args, args[1:], strict=False) if flag == "-f")
    if kind == "milestones":
        titles = query.get("title", [""])
        print(json.dumps([m for m in found.get("milestones", []) if m["title"] in titles or titles == [""]]))
    elif "-X" in args:
        if data["create_fails"] > 0:
            data["create_fails"] -= 1
            (root / "state.json").write_text(json.dumps(data))
            fail("glab: 502 Bad Gateway")
        iid = max((int(n) for n in found["items"]), default=0) + 1
        found["items"][str(iid)] = {
            "title": fields["title"],
            "description": fields["description"],
            "state": "open",
            "type": "Issue",
            "labels": [name for name in fields.get("labels", "").split(",") if name],
            "milestone_id": int(fields["milestone_id"]) if "milestone_id" in fields else None,
            "children": [],
            "blocked_by": [],
        }
        print(json.dumps({"iid": iid, "title": fields["title"]}))
    else:
        wanted = [name for name in query.get("labels", [""])[0].split(",") if name]
        print(
            json.dumps(
                [
                    {"iid": int(iid), "title": item["title"], "description": item["description"]}
                    for iid, item in found["items"].items()
                    if item["state"] == "open" and set(wanted) <= set(item.get("labels", []))
                ]
            )
        )


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
    if command == "mr" and args[0] == "update":
        flags = dict(zip(args[2::2], args[3::2], strict=True))
        data["merge_requests"][int(args[1].rsplit("/", 1)[1]) - 1]["body"] = flags["--description"]
        (root / "state.json").write_text(json.dumps(data))
        return
    if command == "mr" and args[0] == "merge":
        try:
            mr_merge(data, args[1], args[2:])
        finally:
            (root / "state.json").write_text(json.dumps(data))
        return
    if command == "auth" and args[:2] == ["status", "--hostname"]:
        if args[2] in data["signed_out"] or args[2] not in data["hosts"]:
            fail(f"x No token found for {args[2]}")
        return
    if (
        command == "api"
        and args[0].startswith("projects/")
        and not any(
            pattern.match(args[0])
            for pattern in (SEARCH, ISSUE, MERGE_REQUEST, DISCUSSIONS, LABEL_EVENTS, MEMBERS, JOBS, TRACE)
        )
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
    if "user" in args:
        print(json.dumps(data["user"]))
        return
    [path] = [arg for arg in args if arg.startswith("projects/")]
    if path.partition("?")[0].endswith("/labels"):
        project = unquote(path.partition("?")[0].removeprefix("projects/").removesuffix("/labels"))
        if project in data["unreadable"]:
            fail("glab: 404 Project Not Found")
        print(json.dumps([{"name": name} for name in data["repo_labels"].get(project, [])]))
    elif DISCUSSIONS.fullmatch(path):
        discussions(data, path)
    elif LABEL_EVENTS.fullmatch(path):
        label_events(data, path)
    elif MEMBERS.fullmatch(path):
        members = data.get("members", {"factory-bot": 40, "alice": 30})
        print(json.dumps([{"username": name, "access_level": level} for name, level in members.items()]))
    elif SEARCH.fullmatch(path):
        search(data, path)
    elif MERGE_REQUEST.fullmatch(path):
        merge_request(data, path)
    elif JOBS.fullmatch(path):
        print(json.dumps([{k: v for k, v in job.items() if k != "trace"} for job in data.get("jobs", [])]))
    elif match := TRACE.fullmatch(path):
        print(next(job["trace"] for job in data.get("jobs", []) if str(job["id"]) == match.group(1)))
    elif COLLECTION.fullmatch(path):
        serve_collection(root, data, path, args)
    else:
        write_issue(data, path, args)
    (root / "state.json").write_text(json.dumps(data))


if __name__ == "__main__":
    main()
