"""Fake `glab` CLI that stands in for GitLab with issues, child Tasks, blocked-by links and merge requests.

It takes its state directory first and the `glab` arguments after it.
`state.json` there holds the projects of each host, whether every call fails as `glab` without access does,
and whether the instance has the "is blocked by" links of Premium (`linked_items`),
and `calls.jsonl` records every call.
It also takes notes (`-X POST projects/<path>/issues/<iid>/notes -f body=`) and label changes
(`-X PUT projects/<path>/issues/<iid> -f add_labels=` or `-f remove_labels=`) on an issue,
which it keeps on the work item as `notes` and `labels`; a label already there or gone is no error, as in GitLab.
It answers `api graphql --hostname <host> -f query=...` the way GitLab's work item GraphQL API does:
a work item has the widgets `DESCRIPTION`, `HIERARCHY` and, on Premium and up, `LINKED_ITEMS`,
a state of `OPEN` or `CLOSED`, and an unknown project comes back as `{"data": {"project": null}}`.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import unquote

INITIAL: dict[str, Any] = {"hosts": {}, "denied": False, "linked_items": False, "children_have_next_page": False}

PROJECT = re.compile(r'project\(fullPath: ("(?:[^"\\]|\\.)*")\)')
IID = re.compile(r'workItems\(iid: ("(?:[^"\\]|\\.)*")\)')
ISSUE = re.compile(r"projects/([^/]+)/issues/(\d+)(/notes)?")


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
    write_issue(data, path, args)
    (root / "state.json").write_text(json.dumps(data))


if __name__ == "__main__":
    main()
