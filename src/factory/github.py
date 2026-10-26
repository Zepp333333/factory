"""GitHubTracker reads a Spec from GitHub Issues and lands a Run's Integration branch there as a PR.

It calls the owner's `gh` on the host, and no agent in a Sandbox ever reaches GitHub.
"""

import json
import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.states import TicketState
from factory.tracker import PullRequest, SpecSnapshot, TicketSnapshot, TrackerError, not_landed

ISSUE_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)/?")
REMOTE_URL = re.compile(r"(?:[a-z+]+://)?(?:[^@/]+@)?github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?")
API_REPO = re.compile(r"https://api\.github\.com/repos/([\w.-]+/[\w.-]+)")

# GitHub closes an issue on merge when a PR body puts one of these keywords before the issue's reference.
CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
    r"(?=\s*:?\s*(?:(?:[\w.-]+/[\w.-]+)?#\d|https://github\.com/[\w.-]+/[\w.-]+/issues/\d))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Repository:
    owner: str
    name: str

    def __str__(self) -> str:
        return f"{self.owner}/{self.name}"

    def is_named(self, name: str) -> bool:
        return name.lower() == str(self).lower()

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

    def __init__(self, link: str, tickets: Sequence[int] | None = None, command: Sequence[str] = ("gh",)) -> None:
        match = ISSUE_URL.fullmatch(link)
        if match is None:
            raise TrackerError(f"{link} is not a link to a GitHub issue")
        self.link = link
        self.repo = Repository(match.group(1), match.group(2))
        self.number = int(match.group(3))
        self.selected = list(dict.fromkeys(tickets)) if tickets is not None else None
        self.command = tuple(command)

    @property
    def pull_requests(self) -> "GitHubTracker":
        return self

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
        return SpecSnapshot(ref=self.link, title=spec["title"], body=_text(spec["body"]), tickets=tickets)

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
            id=str(issue["number"]), title=issue["title"], body=_text(issue["body"]), blocked_by=blocked_by
        )

    def describe(
        self, snapshot: SpecSnapshot, run_id: str, states: dict[str, TicketState], kept: dict[str, str]
    ) -> PullRequest:
        """Writes the PR that closes the merged Tickets and lists the rest with why they did not land."""
        intro = f"Lands Spec #{self.number} from Run {run_id}."
        return PullRequest(title=snapshot.title, body=f"{intro}\n\n{self.footer(snapshot, states, kept)}")

    def footer(self, snapshot: SpecSnapshot, states: dict[str, TicketState], kept: dict[str, str]) -> str:
        closes = "".join(f"Closes #{t.id}\n" for t in snapshot.tickets if states[t.id] == TicketState.MERGED)
        return closes + not_landed(snapshot, states, kept, mark="#")

    def defuse(self, text: str) -> str:
        """Turns each closing keyword before an issue reference into `Refs`, which links the issue and keeps it open."""
        return CLOSING.sub("Refs", text)

    def accepts(self, remote_url: str) -> bool:
        return self.repo.is_remote(remote_url)

    def url(self) -> str:
        return f"https://github.com/{self.repo}.git"

    def open(self, head: str, base: str, title: str, body: Path) -> str:
        """Opens the PR and returns the URL `gh` prints last."""
        printed = self._gh(*self._create(head, base, title, body)).split()
        if not printed:
            raise TrackerError("gh pr create printed no URL")
        return printed[-1]

    def open_command(self, head: str, base: str, title: str, body: Path) -> list[str]:
        """Returns the `gh` command that opens the PR, which the owner can also run by hand."""
        return ["gh", *self._create(head, base, title, body)]

    def _create(self, head: str, base: str, title: str, body: Path) -> list[str]:
        flags = {"--repo": str(self.repo), "--base": base, "--head": head, "--title": title, "--body-file": str(body)}
        return ["pr", "create", *(arg for item in flags.items() for arg in item)]

    def _ours(self, issue: dict[str, Any]) -> bool:
        return self.repo.is_named(_repo_of(issue))

    def _name(self, issue: dict[str, Any]) -> str:
        return f"#{issue['number']}" if self._ours(issue) else f"{_repo_of(issue)}#{issue['number']}"

    def _api(self, path: str) -> dict[str, Any]:
        data = _decode(self._gh("api", path))
        if len(data) != 1 or not isinstance(data[0], dict):
            raise TrackerError(f"gh api {path} gave no issue")
        return data[0]

    def _pages(self, path: str) -> list[dict[str, Any]]:
        """Reads every page of a list, which `gh api --paginate` prints as JSON arrays back to back."""
        pages = _decode(self._gh("api", "--paginate", f"{path}?per_page=100"))
        if not all(isinstance(page, list) for page in pages):
            raise TrackerError(f"gh api {path} gave no list")
        return [item for page in pages for item in page]

    def _gh(self, *args: str) -> str:
        env = {**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
        try:
            proc = subprocess.run(
                [*self.command, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env
            )
        except OSError as error:
            raise TrackerError(f"cannot run gh: {error}") from error
        if proc.returncode != 0:
            message = proc.stderr.strip().splitlines()
            raise TrackerError(f"gh {args[0]} failed: {message[0] if message else f'exit code {proc.returncode}'}")
        return proc.stdout


def _repo_of(issue: dict[str, Any]) -> str:
    match = API_REPO.fullmatch(str(issue.get("repository_url", "")))
    return match.group(1) if match else "?"


def _decode(text: str) -> list[Any]:
    """Decodes one or more JSON values printed back to back."""
    decoder = json.JSONDecoder()
    values, at = [], 0
    try:
        while (at := _skip_space(text, at)) < len(text):
            value, at = decoder.raw_decode(text, at)
            values.append(value)
    except json.JSONDecodeError as error:
        raise TrackerError(f"gh gave no JSON: {text.strip()[:200]}") from error
    return values


def _skip_space(text: str, at: int) -> int:
    while at < len(text) and text[at].isspace():
        at += 1
    return at


def _text(body: Any) -> str:
    return str(body or "").replace("\r\n", "\n").strip()
