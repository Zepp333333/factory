"""GitHubTracker reads a Spec from GitHub Issues, and GitHubPullRequests lands a Run's Integration branch as a PR there.

It calls the owner's `gh` on the host, and no agent in a Sandbox ever reaches GitHub.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any
from urllib.parse import quote

from factory.adapters.tracker_cli import (
    CLI_TIMEOUT_SECONDS,
    PROJECTION_TIMEOUT_SECONDS,
    TrackerCli,
    comment_of,
    issue_numbers,
    signed,
    tail_of,
    text_of,
)
from factory.core.states import TicketState
from factory.ports.kind import CheckContext, Kind, Problems, Tools
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
    merged_title,
    not_landed,
)

ISSUE_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)/?")
REMOTE_URL = re.compile(r"(?:[a-z+]+://)?(?:[^@/]+@)?github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?")
JOB_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+/job/(\d+)")
PULL_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)/?")
API_REPO = re.compile(r"https://api\.github\.com/repos/([\w.-]+/[\w.-]+)")

# GitHub closes an issue on merge when a PR body puts one of these keywords before the issue's reference.
CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
    r"(?=\s*:?\s*(?:(?:[\w.-]+/[\w.-]+)?#\d|https://github\.com/[\w.-]+/[\w.-]+/issues/\d))",
    re.IGNORECASE,
)

GH_ENV = {"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str

    def __str__(self) -> str:
        return f"{self.owner}/{self.name}"

    def is_named(self, name: str) -> bool:
        return name.lower() == str(self).lower()

    def url(self) -> str:
        return f"https://github.com/{self}.git"

    def is_remote(self, url: str) -> bool:
        """Tells whether a git remote URL, over HTTPS or SSH, points at this repository on github.com."""
        match = REMOTE_URL.fullmatch(url.strip())
        return match is not None and self.is_named(f"{match.group(1)}/{match.group(2)}")


def is_issue_link(ref: str) -> bool:
    return ref.startswith(("https://github.com/", "http://github.com/", "github.com/"))


class GitHubTracker:
    """GitHubTracker reads the Spec behind an issue link, with its native sub-issues as Tickets.

    A Ticket's id is its issue number, and its native blocked-by links become `blocked_by`.
    Without `tickets` the Run takes every open sub-issue, and with it only those numbers, each open.
    A closed blocker counts as done and stays out of the snapshot, while an open blocker
    outside the Run stops it before it starts.
    """

    def __init__(self, link: str, tickets: Sequence[str] | None = None, command: Sequence[str] = ("gh",)) -> None:
        match = ISSUE_URL.fullmatch(link)
        if match is None:
            raise TrackerError(f"{link} is not a link to a GitHub issue")
        self.link = link
        self.repo = Repository(match.group(1), match.group(2))
        self.number = int(match.group(3))
        self.selected = issue_numbers(tickets)
        self.cli = TrackerCli("gh", command, GH_ENV)

    def read_spec(self) -> SpecSnapshot:
        spec = self._api(f"repos/{self.repo}/issues/{self.number}")
        children = self._pages(f"repos/{self.repo}/issues/{self.number}/sub_issues")
        for child in children:
            if not self._ours(child):
                raise TrackerError(f"sub-issue {self._name(child)} of {self.link} lives in another repository")
        by_number = {int(child["number"]): child for child in children}
        if self.selected is None:
            taken = [child for child in children if child["state"] == "open"]
            if not taken:
                raise TrackerError(f"{self.link} has no open sub-issues")
        else:
            for number in self.selected:
                if number not in by_number:
                    raise TrackerError(f"#{number} is not a sub-issue of {self.link}")
                if by_number[number]["state"] != "open":
                    raise TrackerError(f"#{number} is closed, so this Run cannot take it")
            taken = [by_number[number] for number in self.selected]
        numbers = {int(child["number"]) for child in taken}
        tickets = [self._ticket(child, numbers) for child in taken]
        return SpecSnapshot(ref=self.link, title=spec["title"], body=text_of(spec["body"]), tickets=tickets)

    def _ticket(self, issue: dict[str, Any], taken: set[int]) -> TicketSnapshot:
        blocked_by = []
        for blocker in self._pages(f"repos/{self.repo}/issues/{issue['number']}/dependencies/blocked_by"):
            if blocker["state"] != "open":
                continue
            if not self._ours(blocker) or int(blocker["number"]) not in taken:
                raise TrackerError(
                    f"#{issue['number']} is blocked by open {self._name(blocker)}, which this Run does not take"
                )
            blocked_by.append(str(blocker["number"]))
        return TicketSnapshot(
            id=str(issue["number"]), title=issue["title"], body=text_of(issue["body"]), blocked_by=blocked_by
        )

    def describe(
        self, snapshot: SpecSnapshot, run_id: str, states: dict[str, TicketState], kept: dict[str, str]
    ) -> PullRequest:
        """Writes the PR that closes the merged Tickets and lists the rest with why they did not land."""
        intro = f"Lands Spec #{self.number} from Run {run_id}."
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
        """Turns each closing keyword before an issue reference into `Refs`, which links the issue and keeps it open."""
        return CLOSING.sub("Refs", text)

    @property
    def pull_request_folder(self) -> None:
        return None

    def code_url(self) -> str:
        """Returns the git URL of the repository the Spec's issue lives in."""
        return self.repo.url()

    def comment(self, ticket: str, key: int, text: str) -> None:
        self.cli.run(
            "api",
            f"repos/{self.repo}/issues/{ticket}/comments",
            "-f",
            f"body={signed(text)}",
            timeout=PROJECTION_TIMEOUT_SECONDS,
        )

    def add_label(self, ticket: str, label: str) -> None:
        self.cli.run(
            "api",
            f"repos/{self.repo}/issues/{ticket}/labels",
            "-f",
            f"labels[]={label}",
            timeout=PROJECTION_TIMEOUT_SECONDS,
        )

    def remove_label(self, ticket: str, label: str) -> None:
        try:
            path = f"repos/{self.repo}/issues/{ticket}/labels/{quote(label, safe='')}"
            self.cli.run("api", "-X", "DELETE", path, timeout=PROJECTION_TIMEOUT_SECONDS)
        except TrackerError as error:
            # GitHub answers a label the issue does not hold with `Label does not exist (HTTP 404)`.
            if "HTTP 404" not in str(error):
                raise

    def spec_id(self) -> str:
        return str(self.number)

    def assign(self, ticket: str) -> str | None:
        path = f"repos/{self.repo}/issues/{ticket}"
        if self.account in _assignees(self._api(path, timeout=PROJECTION_TIMEOUT_SECONDS)):
            return None
        fields = ("-f", f"assignees[]={self.account}")
        issue = self._api(f"{path}/assignees", *fields, timeout=PROJECTION_TIMEOUT_SECONDS)
        # GitHub answers an assignee it does not take, for lack of rights, with the issue without them and no error.
        if self.account not in _assignees(issue):
            raise TrackerError(f"GitHub did not assign #{ticket} to {self.account}")
        return self.account

    @cached_property
    def account(self) -> str:
        """Returns the login of the user `gh` is logged in as."""
        login = self._api("user", timeout=PROJECTION_TIMEOUT_SECONDS).get("login")
        if not login:
            raise TrackerError("gh api user gave no login")
        return str(login)

    def owner(self) -> str:
        return self.account

    def ticket_comments(self, ticket: str) -> list[Comment]:
        return self._conversation(str(self.repo), ticket)

    def pr_comments(self, pr: str) -> list[Comment]:
        """Reads the conversation of the PR, the text of its reviews and its review comments, each with its own ids.

        A review comment's thread is the one it answers, or its own, and its line is the one it was written on once the
        diff moved past it. A review counts once it is submitted and holds text.
        """
        match = PULL_URL.fullmatch(pr)
        if match is None:
            raise TrackerError(f"{pr} is not a link to a GitHub pull request")
        repo, number = match.groups()
        path = f"repos/{repo}/pulls/{number}"
        reviews = [
            comment_of(f"review-{item['id']}", _login(item), item["body"])
            for item in self._discussion(f"{path}/reviews")
            if item["body"] and item["state"] != "PENDING"
        ]
        threads = [
            comment_of(
                f"thread-{item['id']}",
                _login(item),
                item["body"],
                path=item["path"],
                line=item.get("line") or item.get("original_line"),
                thread=str(item.get("in_reply_to_id") or item["id"]),
            )
            for item in self._discussion(f"{path}/comments")
        ]
        return [*self._conversation(repo, number), *reviews, *threads]

    def _conversation(self, repo: str, number: str) -> list[Comment]:
        """Reads the comments of an issue, which a PR's conversation is too."""
        return [
            comment_of(f"comment-{item['id']}", _login(item), item["body"])
            for item in self._discussion(f"repos/{repo}/issues/{number}/comments")
        ]

    def _discussion(self, path: str) -> list[dict[str, Any]]:
        return self._pages(path, timeout=PROJECTION_TIMEOUT_SECONDS)

    def find_issues(self, labels: Sequence[str]) -> list[Issue]:
        # The issues endpoint lists the repository's PRs too, and only a PR carries `pull_request`.
        query = "state=open" + (f"&labels={quote(','.join(labels), safe=',')}" if labels else "")
        found = self._pages(f"repos/{self.repo}/issues", query)
        return [
            Issue(str(item["number"]), item["title"], text_of(item["body"]))
            for item in found
            if "pull_request" not in item
        ]

    def create_issue(self, title: str, body: str, labels: Sequence[str], milestone: str | None) -> str:
        fields = ["-f", f"title={title}", "-f", f"body={body}"]
        fields += [arg for label in labels for arg in ("-f", f"labels[]={label}")]
        if milestone is not None:
            fields += ["-F", f"milestone={self._milestone(milestone)}"]
        data = self.cli.decode(self.cli.run("api", f"repos/{self.repo}/issues", *fields))
        number = data[0].get("number") if len(data) == 1 and isinstance(data[0], dict) else None
        if not isinstance(number, int):
            raise TrackerError("gh api gave no number for the issue it filed")
        return str(number)

    def _milestone(self, title: str) -> int:
        """Returns the number of the open milestone named `title`."""
        for item in self._pages(f"repos/{self.repo}/milestones", "state=open"):
            if item["title"] == title:
                return int(item["number"])
        raise TrackerError(f"{self.repo} has no open milestone {title}")

    def _ours(self, issue: dict[str, Any]) -> bool:
        return self.repo.is_named(_repo_of(issue))

    def _name(self, issue: dict[str, Any]) -> str:
        return f"#{issue['number']}" if self._ours(issue) else f"{_repo_of(issue)}#{issue['number']}"

    def _api(self, path: str, *fields: str, timeout: float = CLI_TIMEOUT_SECONDS) -> dict[str, Any]:
        data = self.cli.decode(self.cli.run("api", path, *fields, timeout=timeout))
        if len(data) != 1 or not isinstance(data[0], dict):
            raise TrackerError(f"gh api {path} gave no object")
        return data[0]

    def _pages(self, path: str, query: str = "", timeout: float = CLI_TIMEOUT_SECONDS) -> list[dict[str, Any]]:
        """Reads every page of a list, which `gh api --paginate` prints as JSON arrays back to back."""
        url = f"{path}?{query + '&' if query else ''}per_page=100"
        pages = self.cli.decode(self.cli.run("api", "--paginate", url, timeout=timeout))
        if not all(isinstance(page, list) for page in pages):
            raise TrackerError(f"gh api {path} gave no list")
        return [item for page in pages for item in page]


def _assignees(issue: dict[str, Any]) -> list[str]:
    return [str(user["login"]) for user in issue.get("assignees") or []]


def _login(item: dict[str, Any]) -> str:
    """Returns who wrote a comment or a review, which is nobody for a deleted account."""
    return str((item.get("user") or {}).get("login", ""))


def is_remote_url(url: str) -> bool:
    """Tells whether a git remote URL, over HTTPS or SSH, points at a repository on github.com."""
    return REMOTE_URL.fullmatch(url.strip()) is not None


class GitHubPullRequests:
    """GitHubPullRequests lands a Run as a PR in the GitHub repository a git remote points at, through `gh`."""

    pushes = True
    # The CI config of the code host: a fix that changes it would get green CI by changing what CI checks.
    ci_config_paths = (".github/",)

    def __init__(self, repo: Repository, command: Sequence[str] = ("gh",)) -> None:
        self.repo = repo
        self.cli = TrackerCli("gh", command, GH_ENV)

    @classmethod
    def of_remote(cls, url: str, command: Sequence[str] = ("gh",)) -> "GitHubPullRequests":
        match = REMOTE_URL.fullmatch(url.strip())
        if match is None:
            raise TrackerError(f"{url} is not a git remote on github.com")
        return cls(Repository(match.group(1), match.group(2)), command)

    def accepts(self, remote_url: str) -> bool:
        return self.repo.is_remote(remote_url)

    def url(self) -> str:
        return self.repo.url()

    def open(self, head: str, base: str, title: str, body: Path, layer: int | None = None) -> str:
        """Opens the PR and returns the URL `gh` prints last."""
        printed = self.cli.run(*self._create(head, base, title, body)).split()
        if not printed:
            raise TrackerError("gh pr create printed no URL")
        return printed[-1]

    def open_command(self, head: str, base: str, title: str, body: Path) -> list[str]:
        """Returns the `gh` command that opens the PR, which the owner can also run by hand."""
        return ["gh", *self._create(head, base, title, body)]

    def _create(self, head: str, base: str, title: str, body: Path) -> list[str]:
        flags = {"--repo": str(self.repo), "--base": base, "--head": head, "--title": title, "--body-file": str(body)}
        return ["pr", "create", *(arg for item in flags.items() for arg in item)]

    def edit(self, pr: str, body: Path) -> None:
        self.cli.run("pr", "edit", pr, "--repo", str(self.repo), "--body-file", str(body))

    def head(self, pr: str) -> str:
        data = self.cli.decode(self.cli.run("pr", "view", pr, "--json", "headRefOid"))
        oid = data[0].get("headRefOid") if len(data) == 1 and isinstance(data[0], dict) else None
        if not oid:
            raise TrackerError(f"gh pr view gave no head commit of {pr}")
        return str(oid)

    def ci(self, pr: str, commit: str) -> CiStatus:
        """Reads the checks of the PR through `gh pr checks`, which reports those of the PR's head commit.

        Another head than `commit` means the checks are not the commit's, so none count and the CI is still pending.
        """
        if self.head(pr) != commit:
            return CiStatus("pending")
        try:
            data = self.cli.decode(self.cli.run("pr", "checks", pr, "--json", "name,bucket,link"))
        except TrackerError as error:
            if "no checks reported" in str(error):
                return CiStatus("none")
            raise
        checks = data[0] if len(data) == 1 and isinstance(data[0], list) else None
        if checks is None:
            raise TrackerError(f"gh pr checks gave no list for {pr}")
        buckets = {check["bucket"] for check in checks}
        if not checks or buckets <= {"skipping"}:
            return CiStatus("none")
        if "pending" in buckets:
            return CiStatus("pending")
        failed = [check for check in checks if check["bucket"] in ("fail", "cancel")]
        if not failed:
            return CiStatus("passed")
        return CiStatus("failed", [self._failed(check["name"], check["link"]) for check in failed])

    def _failed(self, name: str, url: str) -> FailedJob:
        """Reads the log of the failed steps of the job behind the check, which only a GitHub Actions job has."""
        match = JOB_URL.match(url)
        if match is None:
            return FailedJob(name, url)
        log = self.cli.run("run", "view", "--repo", str(self.repo), "--job", match.group(1), "--log-failed")
        return FailedJob(name, url, tail_of(log))


def _repo_of(issue: dict[str, Any]) -> str:
    match = API_REPO.fullmatch(str(issue.get("repository_url", "")))
    return match.group(1) if match else "?"


def _tracker(link: SpecLink, tools: Tools) -> Tracker:
    """Makes the GitHubTracker of an issue link, which serves every role of a Tracker."""
    tracker = GitHubTracker(link.spec, link.tickets)
    return Tracker(tracker, tracker, tracker, tracker, tracker)


def _check(link: SpecLink, context: CheckContext) -> Problems:
    """Refuses Ticket ids that are not issue numbers."""
    try:
        issue_numbers(link.tickets)
    except TrackerError as error:
        return Problems(errors=(str(error),))
    return Problems()


def _pull_requests(url: str, tools: Tools) -> GitHubPullRequests:
    return GitHubPullRequests.of_remote(url)


TRACKER = Kind[Tracker](_tracker, check=_check, picks=is_issue_link)
PULL_REQUESTS = Kind[GitHubPullRequests](_pull_requests, picks=is_remote_url)
