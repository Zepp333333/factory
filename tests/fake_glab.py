"""Fake `glab` CLI that stands in for GitLab with issues, child Tasks, blocked-by links and merge requests.

It takes its state directory first and the `glab` arguments after it.
`state.json` there holds the projects of each host, whether every call fails as `glab` without access does,
and whether the instance has the "is blocked by" links of Premium (`linked_items`),
and `calls.jsonl` records every call.
It answers `api graphql --hostname <host> -f query=...` the way GitLab's work item GraphQL API does:
a work item has the widgets `DESCRIPTION`, `HIERARCHY` and, on Premium and up, `LINKED_ITEMS`,
a state of `OPEN` or `CLOSED`, and an unknown project comes back as `{"data": {"project": null}}`.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

INITIAL: dict[str, Any] = {"hosts": {}, "denied": False, "linked_items": False, "children_have_next_page": False}

PROJECT = re.compile(r'project\(fullPath: ("(?:[^"\\]|\\.)*")\)')
IID = re.compile(r'workItems\(iid: ("(?:[^"\\]|\\.)*")\)')


def fail(message: str) -> None:
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
    return {"linkType": "blocked_by", "workItem": node}


def graphql(data: dict[str, Any], query: str) -> None:
    project = json.loads(PROJECT.search(query).group(1))  # type: ignore[union-attr]
    iid = json.loads(IID.search(query).group(1))  # type: ignore[union-attr]
    projects = data["hosts"].get(data["host"], {}).get("projects", {})
    if project not in projects:
        print(json.dumps({"data": {"project": None}}))
        return
    nodes = [item(data, project, iid, "children(first" in query)] if iid in projects[project]["items"] else []
    print(json.dumps({"data": {"project": {"workItems": {"nodes": nodes}}}}))


def main() -> None:
    root = Path(sys.argv[1])
    command, *args = sys.argv[2:]
    with (root / "calls.jsonl").open("a") as calls:
        calls.write(json.dumps([command, *args]) + "\n")
    data = json.loads((root / "state.json").read_text())
    if data["denied"]:
        fail("glab: 401 Unauthorized")
    if command != "api" or args[0] != "graphql" or args[1] != "--hostname":
        fail(f"unknown command {command} {' '.join(args)}")
    data["host"] = args[2]
    if args[2] not in data["hosts"]:
        fail(f'Get "https://{args[2]}/api/graphql": dial tcp: lookup {args[2]}: no such host')
    graphql(data, args[args.index("-f") + 1].removeprefix("query="))


if __name__ == "__main__":
    main()
