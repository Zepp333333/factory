"""GitLabTracker reads a Spec from GitLab Issues, on a self-managed instance or on gitlab.com.

It calls the owner's `glab` on the host with the link's host as `--hostname`,
and no agent in a Sandbox ever reaches GitLab.
"""

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from factory import front_matter
from factory.states import TicketState
from factory.tracker import PullRequest, SpecSnapshot, TicketSnapshot, TrackerError, merged_title, not_landed
from factory.tracker_cli import TrackerCli, text_of

ISSUE_URL = re.compile(r"https?://([^/\s]+)/((?:[^/\s]+/)*[^/\s]+)/-/(?:issues|work_items)/(\d+)/?")
REMOTE_URL = re.compile(r"(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?")

# GitLab closes an issue on merge when an MR description puts one of these keywords before the issue's reference.
CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?=\s*:?\s*(?:(?:[\w./-]+)?#\d|https?://\S+/-/issues/\d))",
    re.IGNORECASE,
)

GLAB_ENV = {"NO_PROMPT": "1", "NO_COLOR": "1"}
# One page of children, which a Spec of a Run stays well below.
PAGE_SIZE = 100

WORK_ITEM = """
iid title state workItemType { name } namespace { fullPath }
widgets {
  type
  ... on WorkItemWidgetDescription { description }
  ... on WorkItemWidgetLinkedItems {
    linkedItems { nodes { linkType workItem { iid state namespace { fullPath } } } }
  }
}
"""

SPEC_QUERY = f"""
query {{
  project(fullPath: {{path}}) {{
    workItems(iid: {{iid}}) {{
      nodes {{
        {WORK_ITEM}
        widgets {{
          ... on WorkItemWidgetHierarchy {{
            children(first: {PAGE_SIZE}) {{ pageInfo {{ hasNextPage }} nodes {{ {WORK_ITEM} }} }}
          }}
        }}
      }}
    }}
  }}
}}
"""

ITEM_QUERY = f"""
query {{
  project(fullPath: {{path}}) {{ workItems(iid: {{iid}}) {{ nodes {{ {WORK_ITEM} }} }} }}
}}
"""


def is_issue_link(ref: str) -> bool:
    return ISSUE_URL.fullmatch(ref) is not None


class GitLabTracker:
    """GitLabTracker reads the Spec behind an issue link, with its child Tasks as Tickets.

    A Ticket's id is its `iid`.
    Its `blocked_by` joins the list in the YAML front matter of its description, which the Free tier needs,
    with its native "is blocked by" links where the instance has them.
    Without `tickets` the Run takes every open child Task, and with it only those numbers, each open.
    A closed blocker counts as done and stays out of the snapshot, while an open blocker
    outside the Run stops it before it starts.
    The front matter does not reach the Ticket's body.
    """

    def __init__(self, link: str, tickets: Sequence[int] | None = None, command: Sequence[str] = ("glab",)) -> None:
        match = ISSUE_URL.fullmatch(link)
        if match is None:
            raise TrackerError(f"{link} is not a link to a GitLab issue")
        self.link = link
        self.host = match.group(1)
        self.project = match.group(2)
        self.iid = int(match.group(3))
        self.selected = list(dict.fromkeys(tickets)) if tickets is not None else None
        self.cli = TrackerCli("glab", command, GLAB_ENV)

    @property
    def pull_requests(self) -> "GitLabTracker":
        return self

    def read_spec(self) -> SpecSnapshot:
        spec = self._query(SPEC_QUERY, self.iid)
        if spec is None:
            raise TrackerError(f"{self.link} is not an issue of {self.project} on {self.host}")
        hierarchy = _widget(spec, "children", {"pageInfo": {"hasNextPage": False}, "nodes": []})
        if hierarchy["pageInfo"]["hasNextPage"]:
            raise TrackerError(f"{self.link} has more than {PAGE_SIZE} children")
        children = [child for child in hierarchy["nodes"] if child["workItemType"]["name"] == "Task"]
        by_iid = {int(child["iid"]): child for child in children}
        if self.selected is None:
            taken = [child for child in children if _open(child)]
            if not taken:
                raise TrackerError(f"{self.link} has no open child Tasks")
        else:
            for iid in self.selected:
                if iid not in by_iid:
                    raise TrackerError(f"#{iid} is not a child Task of {self.link}")
                if not _open(by_iid[iid]):
                    raise TrackerError(f"#{iid} is closed, so this Run cannot take it")
            taken = [by_iid[iid] for iid in self.selected]
        numbers = {int(child["iid"]) for child in taken}
        tickets = [self._ticket(child, numbers) for child in taken]
        return SpecSnapshot(
            ref=self.link, title=spec["title"], body=text_of(_widget(spec, "description")), tickets=tickets
        )

    def _ticket(self, task: dict[str, Any], taken: set[int]) -> TicketSnapshot:
        iid = int(task["iid"])
        name = f"#{iid} {task['title']}"
        try:
            meta, body = front_matter.split(text_of(_widget(task, "description")))
        except front_matter.FrontMatterError as error:
            raise TrackerError(f"{name}: {error}") from error
        blocked_by: list[int] = []
        for ref in self._listed(name, meta.get("blocked_by", [])):
            if ref != iid and ref not in blocked_by:
                blocked_by.append(ref)
        for blocker in self._native(task):
            if blocker not in blocked_by:
                blocked_by.append(blocker)
        open_blockers = [ref for ref in blocked_by if self._is_open(name, ref)]
        outside = [ref for ref in open_blockers if ref not in taken]
        if outside:
            raise TrackerError(f"{name} is blocked by open #{outside[0]}, which this Run does not take")
        return TicketSnapshot(
            id=str(iid), title=task["title"], body=body.strip(), blocked_by=[str(ref) for ref in open_blockers]
        )

    def _listed(self, name: str, listed: Any) -> list[int]:
        """Returns the issue numbers of a front matter `blocked_by`, which may write `12` or `#12`."""
        if not isinstance(listed, list):
            raise TrackerError(f"{name}: blocked_by must be a list of issue numbers")
        refs = []
        for item in listed:
            text = str(item).strip().removeprefix("#")
            if isinstance(item, bool) or not text.isdigit():
                raise TrackerError(f"{name}: blocked_by names no issue: {item!r}")
            refs.append(int(text))
        return refs

    def _native(self, task: dict[str, Any]) -> list[int]:
        """Returns the blockers behind "is blocked by" links, which only Premium and up has."""
        links = _widget(task, "linkedItems", {"nodes": []})["nodes"]
        blockers = []
        for link in links:
            if link["linkType"] != "blocked_by":
                continue
            item = link["workItem"]
            if item["namespace"]["fullPath"] != self.project:
                if item["state"] == "OPEN":
                    raise TrackerError(
                        f"#{task['iid']} is blocked by open #{item['iid']} of {item['namespace']['fullPath']},"
                        " which this Run does not take"
                    )
                continue
            blockers.append(int(item["iid"]))
        return blockers

    def _is_open(self, name: str, iid: int) -> bool:
        item = self._query(ITEM_QUERY, iid)
        if item is None:
            raise TrackerError(f"{name}: blocked_by names no issue: #{iid}")
        return _open(item)

    def _query(self, template: str, iid: int) -> dict[str, Any] | None:
        """Returns the work item `iid` of the Spec's project, or None when the project has none."""
        query = template.replace("{path}", json.dumps(self.project)).replace("{iid}", json.dumps(str(iid)))
        data = self.cli.decode(self.cli.run("api", "graphql", "--hostname", self.host, "-f", f"query={query}"))
        if len(data) != 1 or not isinstance(data[0], dict):
            raise TrackerError("glab api graphql gave no answer")
        if data[0].get("errors"):
            raise TrackerError(f"glab api graphql failed: {data[0]['errors'][0].get('message', 'unknown error')}")
        project = (data[0].get("data") or {}).get("project")
        if project is None:
            raise TrackerError(f"{self.host} has no project {self.project}, or the owner's glab cannot see it")
        nodes = project["workItems"]["nodes"]
        return nodes[0] if nodes else None

    def describe(
        self, snapshot: SpecSnapshot, run_id: str, states: dict[str, TicketState], kept: dict[str, str]
    ) -> PullRequest:
        """Writes the MR text that closes the merged Tickets and lists the rest with why they did not land."""
        intro = f"Lands Spec #{self.iid} from Run {run_id}."
        return PullRequest(
            title=merged_title(snapshot, states), body=f"{intro}\n\n{self.footer(snapshot, states, kept)}"
        )

    def footer(self, snapshot: SpecSnapshot, states: dict[str, TicketState], kept: dict[str, str]) -> str:
        closes = "".join(f"Closes #{t.id}\n" for t in snapshot.tickets if states[t.id] == TicketState.MERGED)
        return closes + not_landed(snapshot, states, kept, mark="#")

    def defuse(self, text: str) -> str:
        return CLOSING.sub("Refs", text)

    def accepts(self, remote_url: str) -> bool:
        match = REMOTE_URL.fullmatch(remote_url.strip())
        return match is not None and match.group(1).lower() == self.host.lower() and match.group(2) == self.project

    def url(self) -> str:
        return f"https://{self.host}/{self.project}.git"

    def open(self, head: str, base: str, title: str, body: Path) -> str:
        raise TrackerError("opening a merge request in GitLab is not supported yet")

    def open_command(self, head: str, base: str, title: str, body: Path) -> list[str]:
        """Returns the `glab` command that opens the MR, which the owner can run by hand."""
        flags = {
            "--repo": f"{self.host}/{self.project}",
            "--source-branch": head,
            "--target-branch": base,
            "--title": title,
            "--description": body.read_text(),
        }
        return ["glab", "mr", "create", *(arg for item in flags.items() for arg in item)]

    def comment(self, ticket: str, key: int, text: str) -> None:
        raise TrackerError("projecting into GitLab is not supported yet")

    def add_label(self, ticket: str, label: str) -> None:
        raise TrackerError("projecting into GitLab is not supported yet")

    def remove_label(self, ticket: str, label: str) -> None:
        raise TrackerError("projecting into GitLab is not supported yet")


def _widget(item: dict[str, Any], key: str, default: Any = "") -> Any:
    """Returns `key` from the widget of the work item that holds it, or `default` when the item has no such widget."""
    for widget in item["widgets"]:
        if key in widget and widget[key] is not None:
            return widget[key]
    return default


def _open(item: dict[str, Any]) -> bool:
    return bool(item["state"] == "OPEN")
