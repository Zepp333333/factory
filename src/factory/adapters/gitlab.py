"""GitLabTracker reads a Spec from GitLab Issues, on a self-managed instance or on gitlab.com.

It calls the owner's `glab` on the host with the link's host as `--hostname`,
and no agent in a Sandbox ever reaches GitLab.
"""

import json
import re
import shutil
from collections.abc import Sequence
from functools import cached_property
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from factory.adapters.tracker_cli import (
    PROJECTION_TIMEOUT_SECONDS,
    TrackerCli,
    comment_of,
    issue_numbers,
    oldest_first,
    signed,
    tail_of,
    text_of,
)
from factory.core import front_matter
from factory.core.states import TicketState
from factory.ports.kind import CheckContext, Diagnosis, Kind, Problems, Tools, label_diagnoses
from factory.ports.tracker import (
    CiStatus,
    Comment,
    FailedJob,
    Issue,
    PullRequest,
    SpecLink,
    SpecSnapshot,
    TicketSnapshot,
    Tracker,
    TrackerError,
    TrackerEvent,
    merged_title,
    not_landed,
)

ISSUE_URL = re.compile(r"https?://([^/\s]+)/((?:[^/\s]+/)*[^/\s]+)/-/(?:issues|work_items)/(\d+)/?")
MR_URL = re.compile(r"https?://([^/\s]+)/((?:[^/\s]+/)*[^/\s]+)/-/merge_requests/(\d+)/?")
HTTP_REMOTE_URL = re.compile(r"https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?")
REMOTE_URL = re.compile(r"(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?")

# GitLab closes an issue on merge when an MR description puts one of these keywords before the issue's reference.
CLOSING = re.compile(
    r"\b(?:clos(?:e[sd]?|ing)|fix(?:e[sd]|ing)?|resolv(?:e[sd]?|ing)|implement(?:s|ed|ing)?)"
    r"(?=\s*:?\s*(?:issues?\s+)?(?:[\w./-]*#\d|https?://\S+/-/(?:issues|work_items)/\d))",
    re.IGNORECASE,
)

PART_OF = re.compile(r"Part of #\d+")
BLOCKED_BY = re.compile(r"Blocked by:(.*)")

GLAB_ENV = {"NO_PROMPT": "1", "NO_COLOR": "1"}
# One page of children, which a Spec of a Run stays well below.
PAGE_SIZE = 100

# The access level of a member of a project from which on they can write to its repository.
DEVELOPER = 30
# What GitLab calls an event in the note it writes itself.
SYSTEM_NOTE_EVENTS: dict[str, Literal["approved", "changes_requested"]] = {
    "approved this merge request": "approved",
    "requested changes": "changes_requested",
}
# The note GitLab writes when someone takes back their approval.
UNAPPROVED = "unapproved this merge request"

WORK_ITEM = """
iid title state workItemType { name } namespace { fullPath }
widgets {
  type
  ... on WorkItemWidgetDescription { description }
  ... on WorkItemWidgetLabels { labels { nodes { title } } }
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

    So are the issues of the project whose description opens with the line `Part of #N`,
    which is how a Free-tier project ties Tickets to a Spec without child Tasks.
    The leading lines of a description are its `Part of` and `Blocked by: #a, #b` lines and the blank lines
    before the first other line, and they do not reach the Ticket's body.
    An issue that names the Spec anywhere else is no Ticket.
    A Ticket's id is its `iid`.
    Its `blocked_by` joins the list in the YAML front matter of its description, which the Free tier needs,
    with its native "is blocked by" links where the instance has them.
    Without `tickets` it reads every open child Task, and with it only those numbers, each open.
    A closed blocker counts as done and stays out of the snapshot, while an open one in another project goes in
    `blocked_outside`.
    The front matter does not reach the Ticket's body.
    """

    def __init__(self, link: str, tickets: Sequence[str] | None = None, command: Sequence[str] = ("glab",)) -> None:
        match = ISSUE_URL.fullmatch(link)
        if match is None:
            raise TrackerError(f"{link} is not a link to a GitLab issue")
        self.link = link
        self.host = match.group(1)
        self.hostname = self.host.partition(":")[0]
        self.project = match.group(2)
        self.iid = int(match.group(3))
        self.selected = issue_numbers(tickets)
        self.cli = TrackerCli("glab", command, GLAB_ENV)

    def preflight(self, remotes: dict[str, str], repo: Path) -> list[str]:
        """Returns what keeps `glab` from serving a Run of this Spec, each problem with the command that fixes it.

        `remotes` holds the URLs of the git remotes of the Target project at `repo`.
        """
        problems = []
        if shutil.which(self.cli.command[0]) is None:
            problems.append("glab is not installed: install it, see https://gitlab.com/gitlab-org/cli#installation")
        else:
            problems += self._access()
        if not any(self.remote_points_here(url) for url in remotes.values()):
            problems.append(
                f"no git remote of {repo} points at {self.code_url()}: run `git remote add gitlab {self.code_url()}`"
            )
        return problems

    def _access(self) -> list[str]:
        try:
            self.cli.run("auth", "status", "--hostname", self.host)
        except TrackerError as error:
            return [f"glab is not signed in to {self.host} ({error}): run `glab auth login --hostname {self.host}`"]
        try:
            self.cli.run("api", f"projects/{quote(self.project, safe='')}", "--hostname", self.host)
        except TrackerError as error:
            return [
                f"glab cannot read {self.project} on {self.host} ({error}):"
                f" ask for access to the project, or run `glab auth login --hostname {self.host}`"
                " as a user who has it"
            ]
        return []

    def read_spec(self) -> SpecSnapshot:
        spec = self._query(SPEC_QUERY, self.iid)
        if spec is None:
            raise TrackerError(f"{self.link} is not an issue of {self.project} on {self.host}")
        hierarchy = _widget(spec, "children", {"pageInfo": {"hasNextPage": False}, "nodes": []})
        if hierarchy["pageInfo"]["hasNextPage"]:
            raise TrackerError(f"{self.link} has more than {PAGE_SIZE} children")
        children = [child for child in hierarchy["nodes"] if child["workItemType"]["name"] == "Task"]
        listed = {int(child["iid"]) for child in children}
        children += [part for part in self._parts() if int(part["iid"]) not in listed]
        by_iid = {int(child["iid"]): child for child in children}
        if self.selected is None:
            taken = [child for child in children if _open(child)]
            if not taken:
                raise TrackerError(f"{self.link} has no open Tickets")
        else:
            for iid in self.selected:
                if iid not in by_iid:
                    raise TrackerError(f"#{iid} is not a Ticket of {self.link}")
                if not _open(by_iid[iid]):
                    raise TrackerError(f"#{iid} is closed, so this Run cannot take it")
            taken = [by_iid[iid] for iid in self.selected]
        tickets = [self._ticket(child) for child in taken]
        return SpecSnapshot(
            ref=self.link, title=spec["title"], body=text_of(_widget(spec, "description")), tickets=tickets
        )

    def _parts(self) -> list[dict[str, Any]]:
        """Returns the issues whose leading lines carry `Part of #<iid>`, found by a search of every page of the API."""
        line = f"Part of #{self.iid}"
        found: dict[int, dict[str, Any]] = {}
        page = 1
        while True:
            path = f"projects/{quote(self.project, safe='')}/issues"
            query = f"search={quote(line, safe='')}&in=description&per_page={PAGE_SIZE}&page={page}"
            pages = self.cli.decode(self.cli.run("api", f"{path}?{query}", "--hostname", self.host))
            if len(pages) != 1 or not isinstance(pages[0], list):
                raise TrackerError(f"glab api gave no issues of {self.project}")
            if not pages[0]:
                break
            for issue in pages[0]:
                lines, _ = _leading(text_of(issue.get("description")))
                if line in lines and int(issue["iid"]) != self.iid:
                    found[int(issue["iid"])] = issue
            page += 1
        items = [self._query(ITEM_QUERY, iid) for iid in sorted(found)]
        return [item for item in items if item is not None]

    def _ticket(self, task: dict[str, Any]) -> TicketSnapshot:
        iid = int(task["iid"])
        name = f"#{iid} {task['title']}"
        lines, rest = _leading(text_of(_widget(task, "description")))
        try:
            meta, body = front_matter.split(rest)
        except front_matter.FrontMatterError as error:
            raise TrackerError(f"{name}: {error}") from error
        blocked_by: list[int] = []
        for ref in [*self._written(name, lines), *self._listed(name, meta.get("blocked_by", []))]:
            if ref != iid and ref not in blocked_by:
                blocked_by.append(ref)
        native, outside = self._native(task)
        for blocker in native:
            if blocker not in blocked_by:
                blocked_by.append(blocker)
        open_blockers = [ref for ref in blocked_by if self._is_open(name, ref)]
        return TicketSnapshot(
            id=str(iid),
            title=task["title"],
            body=body.strip(),
            blocked_by=[str(ref) for ref in open_blockers],
            labels=[str(node["title"]) for node in _widget(task, "labels", {"nodes": []})["nodes"]],
            blocked_outside=outside,
        )

    def _written(self, name: str, lines: list[str]) -> list[int]:
        """Returns the issue numbers of the `Blocked by: #a, #b` lines among the leading lines."""
        refs = []
        for line in lines:
            if (match := BLOCKED_BY.fullmatch(line)) is None:
                continue
            for item in match.group(1).split(","):
                text = item.strip()
                if re.fullmatch(r"#\d+", text) is None:
                    raise TrackerError(f"{name}: Blocked by names no issue: {text!r}")
                refs.append(int(text[1:]))
        return refs

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

    def _native(self, task: dict[str, Any]) -> tuple[list[int], list[str]]:
        """Returns the blockers behind "is blocked by" links, which only Premium and up has: those of the Spec's project
        by their numbers, and the open ones of another project as they show."""
        links = _widget(task, "linkedItems", {"nodes": []})["nodes"]
        blockers, foreign = [], []
        for link in links:
            if link["linkType"] != "is_blocked_by":
                continue
            item = link["workItem"]
            if item["namespace"]["fullPath"] == self.project:
                blockers.append(int(item["iid"]))
            elif item["state"] == "OPEN":
                foreign.append(f"#{item['iid']} of {item['namespace']['fullPath']}")
        return blockers, foreign

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

    def reference(self, ticket: str) -> str:
        """Shows a Ticket or issue by its number after `#`, which links it."""
        return f"#{ticket}"

    def footer(self, snapshot: SpecSnapshot, states: dict[str, TicketState], kept: dict[str, str]) -> str:
        closes = "".join(
            f"Closes {self.reference(t.id)}\n" for t in snapshot.tickets if states[t.id] == TicketState.MERGED
        )
        return closes + not_landed(snapshot, states, kept, self.reference)

    def defuse(self, text: str) -> str:
        return CLOSING.sub("Refs", text)

    @property
    def pull_request_folder(self) -> None:
        return None

    def code_url(self) -> str:
        """Returns the git URL of the project the Spec's issue lives in."""
        return f"https://{self.host}/{self.project}.git"

    def remote_points_here(self, remote_url: str) -> bool:
        """Tells whether a git remote points at the project the Spec's issue lives in."""
        parsed = parse_remote(remote_url)
        return (
            parsed is not None
            and parsed[0].partition(":")[0].lower() == self.hostname.lower()
            and parsed[1].lower() == self.project.lower()
        )

    def default_branch(self) -> str:
        """Returns the branch GitLab closes issues on a merge into, which the project names."""
        data = self.cli.decode(self.cli.run("api", f"projects/{quote(self.project, safe='')}", "--hostname", self.host))
        branch = data[0].get("default_branch") if len(data) == 1 and isinstance(data[0], dict) else None
        if not branch:
            raise TrackerError(f"glab api gave no default branch of {self.project}")
        return str(branch)

    def comment(self, ticket: str, key: int, text: str) -> None:
        self._rest("POST", ticket, "notes", f"body={signed(text)}")

    def add_label(self, ticket: str, label: str) -> None:
        self._rest("PUT", ticket, "", f"add_labels={label}")

    def remove_label(self, ticket: str, label: str) -> None:
        self._rest("PUT", ticket, "", f"remove_labels={label}")

    def spec_id(self) -> str:
        return str(self.iid)

    def assign(self, ticket: str) -> str | None:
        """Sets the assignees to the Ticket's own with the account after them, since GitLab replaces the whole list."""
        own_id, username = self.account
        path = f"projects/{quote(self.project, safe='')}/issues/{ticket}"
        held = _assignees(self._api(path))
        if own_id in held:
            return None
        query = "&".join(f"assignee_ids[]={assignee}" for assignee in [*held, own_id])
        # GitLab without the rights, or with one assignee per issue, leaves the account off and gives no error.
        if own_id not in _assignees(self._api("-X", "PUT", f"{path}?{query}")):
            raise TrackerError(f"GitLab did not assign #{ticket} to {username}")
        return username

    @cached_property
    def account(self) -> tuple[int, str]:
        """Returns the id and the username of the owner of the token `glab` works with."""
        user = self._api("user")
        if "id" not in user or "username" not in user:
            raise TrackerError("glab api user gave no id and username")
        return int(user["id"]), str(user["username"])

    def owner(self) -> str:
        return self.account[1]

    def ticket_comments(self, ticket: str) -> list[Comment]:
        return self._discussions(self.host, self.project, f"issues/{ticket}")

    def pr_comments(self, pr: str) -> list[Comment]:
        host, project, number = _merge_request_link(pr).groups()
        return self._discussions(host, project, f"merge_requests/{number}")

    def events(self, target: str) -> list[TrackerEvent]:
        return _events_of(self.cli, self.host, self.project, f"issues/{target}")

    def _discussions(self, host: str, project: str, of: str) -> list[Comment]:
        """Reads the notes of an issue or a merge request, without those GitLab writes itself about changes.

        A note on its own has no thread, and only a note on the diff has a place.
        """
        path = f"projects/{quote(project, safe='')}/{of}/discussions"
        return [
            _note(note, None if discussion["individual_note"] else str(discussion["id"]))
            for discussion in _pages(self.cli, host, path)
            for note in discussion["notes"]
            if not note.get("system")
        ]

    def find_issues(self, labels: Sequence[str]) -> list[Issue]:
        query = "state=opened&per_page=100" + (f"&labels={quote(','.join(labels), safe=',')}" if labels else "")
        path = f"projects/{quote(self.project, safe='')}/issues?{query}"
        pages = self.cli.decode(self.cli.run("api", "--hostname", self.host, "--paginate", path))
        if not all(isinstance(page, list) for page in pages):
            raise TrackerError(f"glab api {path} gave no list")
        return [Issue(str(item["iid"]), item["title"], text_of(item["description"])) for page in pages for item in page]

    def create_issue(self, title: str, body: str, labels: Sequence[str], milestone: str | None) -> str:
        fields = ["-f", f"title={title}", "-f", f"description={body}", "-f", f"labels={','.join(labels)}"]
        if milestone is not None:
            fields += ["-f", f"milestone_id={self._milestone(milestone)}"]
        path = f"projects/{quote(self.project, safe='')}/issues"
        data = self.cli.decode(self.cli.run("api", "--hostname", self.host, "-X", "POST", path, *fields))
        iid = data[0].get("iid") if len(data) == 1 and isinstance(data[0], dict) else None
        if not isinstance(iid, int):
            raise TrackerError("glab api gave no iid for the issue it filed")
        return str(iid)

    def _milestone(self, title: str) -> int:
        """Returns the id of the active milestone named `title`."""
        path = f"projects/{quote(self.project, safe='')}/milestones?state=active&title={quote(title, safe='')}"
        data = self.cli.decode(self.cli.run("api", "--hostname", self.host, path))
        found = (
            [item for item in data[0] if item["title"] == title] if len(data) == 1 and isinstance(data[0], list) else []
        )
        if not found:
            raise TrackerError(f"{self.project} has no active milestone {title}")
        return int(found[0]["id"])

    def _api(self, *args: str) -> dict[str, Any]:
        """Calls the REST API for one object, within the time of a projection call."""
        data = self.cli.decode(self.cli.run("api", "--hostname", self.host, *args, timeout=PROJECTION_TIMEOUT_SECONDS))
        if len(data) != 1 or not isinstance(data[0], dict):
            raise TrackerError(f"glab api {args[-1]} gave no object")
        return data[0]

    def _rest(self, method: str, ticket: str, tail: str, field: str) -> None:
        """Calls the REST API of the Ticket's issue; GitLab takes a label already there or gone as no change."""
        path = f"projects/{quote(self.project, safe='')}/issues/{ticket}" + (f"/{tail}" if tail else "")
        self.cli.run(
            "api", "--hostname", self.host, "-X", method, path, "-f", field, timeout=PROJECTION_TIMEOUT_SECONDS
        )


def _pages(cli: TrackerCli, host: str, path: str) -> list[dict[str, Any]]:
    """Reads every page of a list, which `glab api --paginate` prints as JSON arrays back to back."""
    pages = cli.decode(
        cli.run("api", "--hostname", host, "--paginate", f"{path}?per_page=100", timeout=PROJECTION_TIMEOUT_SECONDS)
    )
    if not all(isinstance(page, list) for page in pages):
        raise TrackerError(f"glab api {path} gave no list")
    return [item for page in pages for item in page]


def _events_of(cli: TrackerCli, host: str, project: str, of: str) -> list[TrackerEvent]:
    """Reads the events of an issue or a merge request from its notes and its label changes.

    GitLab tells of an approval and of a request for changes only in a note it writes itself, which carries an id and
    an author where the approvals of a merge request do not.
    A note has no word on how its author relates to the project, so the members of the project tell.
    An approval its author took back later is no event, which GitHub tells by the state of the review.
    """
    base = f"projects/{quote(project, safe='')}"
    writers = {m["username"] for m in _pages(cli, host, f"{base}/members/all") if m["access_level"] >= DEVELOPER}
    found: list[tuple[str, TrackerEvent]] = []
    withdrawn: dict[str, str] = {}
    for discussion in _pages(cli, host, f"{base}/{of}/discussions"):
        for note in discussion["notes"]:
            kind = SYSTEM_NOTE_EVENTS.get(note["body"]) if note.get("system") else "commented"
            written = _note(note, None)
            if note.get("system") and note["body"] == UNAPPROVED:
                withdrawn[written.author] = max(note["created_at"], withdrawn.get(written.author, ""))
            if kind is not None and written.author in writers and not written.by_factory:
                found.append((note["created_at"], TrackerEvent(f"note-{note['id']}", kind, written.author)))
    for change in _pages(cli, host, f"{base}/{of}/resource_label_events"):
        if change["action"] == "add":
            author = str((change.get("user") or {}).get("username", ""))
            found.append((change["created_at"], TrackerEvent(f"label-{change['id']}", "labeled", author)))
    return oldest_first(
        [(at, event) for at, event in found if event.kind != "approved" or at > withdrawn.get(event.author, "")]
    )


def _merge_request_link(pr: str) -> re.Match[str]:
    """Returns the host, the project and the number in a link to a merge request, or raises TrackerError at another."""
    match = MR_URL.fullmatch(pr)
    if match is None:
        raise TrackerError(f"{pr} is not a link to a GitLab merge request")
    return match


def _note(note: dict[str, Any], thread: str | None) -> Comment:
    place = note.get("position") or {}
    return comment_of(
        str(note["id"]),
        note["author"]["username"],
        note["body"],
        path=place.get("new_path") or place.get("old_path"),
        line=place.get("new_line") or place.get("old_line"),
        thread=thread,
    )


def _assignees(issue: dict[str, Any]) -> list[int]:
    return [int(user["id"]) for user in issue.get("assignees") or []]


def parse_remote(url: str) -> tuple[str, str] | None:
    """Returns the host and the project path of a git remote URL, or None when it is not one of a GitLab project.

    The host keeps its port over HTTP(S), where it is the web port, and loses it over SSH.
    """
    match = HTTP_REMOTE_URL.fullmatch(url.strip()) or REMOTE_URL.fullmatch(url.strip())
    return (match.group(1), match.group(2)) if match else None


def is_hosted_remote_url(url: str) -> bool:
    """Tells whether a git remote URL points at a host whose name says GitLab, such as gitlab.com."""
    parsed = parse_remote(url)
    return parsed is not None and "gitlab" in parsed[0].partition(":")[0].lower().split(".")


class GitLabPullRequests:
    """GitLabPullRequests lands a Run as an MR in the GitLab project a git remote points at, through `glab`."""

    pushes = True
    # The CI config of the code host: a fix that changes it would get green CI by changing what CI checks.
    ci_config_paths = (".gitlab/", ".gitlab-ci.yml")

    def __init__(self, host: str, project: str, command: Sequence[str] = ("glab",)) -> None:
        self.host = host
        self.hostname = host.partition(":")[0]
        self.project = project
        self.cli = TrackerCli("glab", command, GLAB_ENV)

    @classmethod
    def of_remote(cls, url: str, command: Sequence[str] = ("glab",)) -> "GitLabPullRequests":
        parsed = parse_remote(url)
        if parsed is None:
            raise TrackerError(f"{url} is not a git remote of a GitLab project")
        return cls(parsed[0], parsed[1], command)

    def diagnose(self, labels: Sequence[str]) -> list[Diagnosis]:
        """Checks that `glab` is signed in to the host, and which of the `labels` the project has."""
        fix = f"glab auth login --hostname {self.hostname}"
        try:
            self.cli.run("auth", "status", "--hostname", self.hostname)
            access = Diagnosis(f"access to {self.project}")
        except TrackerError as error:
            access = Diagnosis(f"access to {self.project}", str(error), fix)
        return [access, *self._labels(labels, fix)]

    def _labels(self, labels: Sequence[str], fix: str) -> list[Diagnosis]:
        if not labels:
            return []
        path = f"projects/{quote(self.project, safe='')}/labels"
        try:
            pages = self.cli.decode(self.cli.run("api", "--hostname", self.host, "--paginate", f"{path}?per_page=100"))
        except TrackerError as error:
            return [Diagnosis(f"labels of {self.project}", str(error), fix)]
        present = {label["name"] for page in pages for label in page}
        repo = f"https://{self.host}/{self.project}"
        return label_diagnoses(
            self.project, labels, present, lambda name: f"glab label create --name {name} --repo {repo}"
        )

    def accepts(self, remote_url: str) -> bool:
        parsed = parse_remote(remote_url)
        return (
            parsed is not None
            and parsed[0].partition(":")[0].lower() == self.hostname.lower()
            and parsed[1].lower() == self.project.lower()
        )

    def url(self) -> str:
        return f"https://{self.host}/{self.project}.git"

    def open(self, head: str, base: str, title: str, body: Path, layer: int | None = None) -> str:
        """Opens the MR and returns the URL `glab` prints last."""
        printed = self.cli.run(*self._create(head, base, title, body)).split()
        if not printed:
            raise TrackerError("glab mr create printed no URL")
        return printed[-1]

    def open_command(self, head: str, base: str, title: str, body: Path) -> list[str]:
        """Returns the `glab` command that opens the MR, which the owner can also run by hand."""
        return ["glab", *self._create(head, base, title, body)]

    def _create(self, head: str, base: str, title: str, body: Path) -> list[str]:
        flags = {
            "--repo": f"{self.host}/{self.project}",
            "--source-branch": head,
            "--target-branch": base,
            "--title": title,
            "--description": body.read_text(),
        }
        return ["mr", "create", *(arg for item in flags.items() for arg in item), "--yes"]

    def edit(self, pr: str, body: Path) -> None:
        self.cli.run("mr", "update", pr, "--repo", f"{self.host}/{self.project}", "--description", body.read_text())

    def head(self, pr: str) -> str:
        sha = self._merge_request(pr).get("sha")
        if not sha:
            raise TrackerError(f"glab api gave no head commit of {pr}")
        return str(sha)

    def merged(self, pr: str) -> bool:
        return self._merge_request(pr).get("state") == "merged"

    def merge(self, pr: str, commit: str, method: str) -> None:
        """Merges the MR at `commit`, where a rebase moves its head first, so `--rebase` cannot name the commit."""
        how = ("--rebase",) if method == "rebase" else ("--sha", commit, *(("--squash",) if method == "squash" else ()))
        self.cli.run("mr", "merge", pr, "--repo", f"{self.host}/{self.project}", "--yes", *how)

    def ci(self, pr: str, commit: str) -> CiStatus:
        """Reads the MR's head pipeline, which counts only when it ran on `commit`.

        A pipeline of another commit does not count, so the CI is still pending.
        A pipeline that stops at a manual job, such as a publish, passed every job that runs on its own,
        so it counts as passed.
        """
        pipeline = self._merge_request(pr).get("head_pipeline")
        if not isinstance(pipeline, dict):
            return CiStatus("none")
        if pipeline.get("sha") != commit:
            return CiStatus("pending")
        match pipeline["status"]:
            case "success" | "manual":
                return CiStatus("passed")
            case "failed" | "canceled":
                return CiStatus("failed", self._failed_jobs(int(pipeline["id"])))
            case _:
                return CiStatus("pending")

    def _merge_request(self, pr: str) -> dict[str, Any]:
        number = _merge_request_link(pr).group(3)
        data = self.cli.decode(self.cli.run("api", self._project_path(f"merge_requests/{number}"), *self._host))
        if len(data) != 1 or not isinstance(data[0], dict):
            raise TrackerError(f"glab api gave no merge request for {pr}")
        return data[0]

    def events(self, target: str) -> list[TrackerEvent]:
        host, project, number = _merge_request_link(target).groups()
        return _events_of(self.cli, host, project, f"merge_requests/{number}")

    def _failed_jobs(self, pipeline: int) -> list[FailedJob]:
        """Lists the failed jobs of the pipeline with the tail of their traces."""
        path = self._project_path(f"pipelines/{pipeline}/jobs?scope[]=failed&per_page=100")
        pages = self.cli.decode(self.cli.run("api", path, *self._host))
        jobs = pages[0] if len(pages) == 1 and isinstance(pages[0], list) else None
        if jobs is None:
            raise TrackerError(f"glab api gave no jobs of pipeline {pipeline}")
        return [
            FailedJob(
                job["name"],
                job["web_url"],
                tail_of(self.cli.run("api", self._project_path(f"jobs/{job['id']}/trace"), *self._host)),
            )
            for job in jobs
        ]

    @property
    def _host(self) -> tuple[str, str]:
        return ("--hostname", self.host)

    def _project_path(self, tail: str) -> str:
        return f"projects/{quote(self.project, safe='')}/{tail}"


def _leading(description: str) -> tuple[list[str], str]:
    """Splits a description into its leading `Part of` and `Blocked by:` lines and the rest after them."""
    lines = description.split("\n")
    for at, line in enumerate(lines):
        text = line.strip()
        if text and not PART_OF.fullmatch(text) and not BLOCKED_BY.fullmatch(text):
            return [item.strip() for item in lines[:at] if item.strip()], "\n".join(lines[at:])
    return [item.strip() for item in lines if item.strip()], ""


def _widget(item: dict[str, Any], key: str, default: Any = "") -> Any:
    """Returns `key` from the widget of the work item that holds it, or `default` when the item has no such widget."""
    for widget in item["widgets"]:
        if key in widget and widget[key] is not None:
            return widget[key]
    return default


def _open(item: dict[str, Any]) -> bool:
    return bool(item["state"] == "OPEN")


def _tracker(link: SpecLink, tools: Tools) -> Tracker:
    """Makes the GitLabTracker of an issue link, which serves every role of a Tracker."""
    tracker = GitLabTracker(link.spec, link.tickets)
    return Tracker(tracker, tracker, tracker, tracker, tracker)


def _check(link: SpecLink, context: CheckContext) -> Problems:
    """Refuses Ticket ids that are not issue numbers, and returns what keeps `glab` from serving a Run of the Spec,
    or else warns when the Run's `base` is not the default branch, because GitLab closes issues only on a merge into
    that one."""
    try:
        tracker = GitLabTracker(link.spec, link.tickets)
    except TrackerError as error:
        return Problems(errors=(str(error),))
    if errors := tracker.preflight(dict(context.remotes), context.repo):
        return Problems(errors=tuple(errors))
    try:
        default = tracker.default_branch()
    except TrackerError as error:
        return Problems(warnings=(f"cannot tell the default branch of {tracker.project}: {error}",))
    if default == context.base:
        return Problems()
    return Problems(
        warnings=(
            f"base {context.base} is not the default branch {default} of {tracker.project},"
            " so GitLab will not close the merged Tickets and they stay open",
        )
    )


def _pull_requests(url: str, tools: Tools) -> GitLabPullRequests:
    return GitLabPullRequests.of_remote(url)


def _diagnose(pull_requests: GitLabPullRequests, labels: Sequence[str]) -> list[Diagnosis]:
    return pull_requests.diagnose(labels)


TRACKER = Kind[Tracker](_tracker, check=_check, picks=is_issue_link)
PULL_REQUESTS = Kind[GitLabPullRequests](_pull_requests, picks=is_hosted_remote_url, diagnose=_diagnose)
