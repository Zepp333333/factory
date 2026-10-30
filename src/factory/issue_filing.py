"""Issue filing is the Operation `file_issues`: it files the issues a Stage reports and numbers them in the code.

No agent in a Sandbox reaches the Tracker, so a Stage writes `#new:<key>` where an issue number goes and lists the issue
under `issues` in its output, and the host files the issues and replaces each placeholder with `#<number>`.
Every issue is journaled with its key before the replacement, and its body carries a hidden marker of the Run, the
Ticket and the key, so a Run that resumes after a crash reuses the issue it filed and never files it twice.
An open issue with the same title and labels is reused as well, which also catches a finding that two Tickets report.
"""

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from factory.artifacts import write_json_once
from factory.attempt import Ports
from factory.events import Event
from factory.git import GitError, HostGit
from factory.live import LiveTicket
from factory.pipeline import Operation
from factory.run_context import RunContext
from factory.tracker import Issue, TrackerError

# A key is lowercase letters, digits and `-`, but a placeholder matches any word, so a malformed key such as
# `#new:foo_bar` fails the Operation as a key without an entry instead of passing through unreplaced.
PLACEHOLDER = re.compile(rb"#new:([\w-]+)")

# A call to the Tracker that failed is tried again after this many seconds, and the Operation fails at the last try.
RETRY_SECONDS = 30
TRIES = 3


class Refused(Exception):
    """Refused is a reason to fail the Operation, with the key and the file it concerns when it has them."""

    def __init__(self, reason: str, key: str | None = None, file: str | None = None) -> None:
        super().__init__(reason)
        self.reason, self.key, self.file = reason, key, file


class IssueFiling:
    """IssueFiling files the issues of a Ticket's Stages and replaces the placeholders in its Workspace.

    `append` journals an event and applies it to the Run state.
    A call to the Tracker that fails is tried again `RETRY_SECONDS` later, as the projection does, and a call
    that fails `TRIES` times in a row ends the Operation in `failed`.
    """

    def __init__(
        self,
        append: Callable[[Event, dict[str, Any]], None],
        ports: Ports,
        git: HostGit,
        context: RunContext,
    ) -> None:
        self.append = append
        self.ports = ports
        self.git = git
        self.context = context
        self.failures: dict[str, tuple[int, datetime]] = {}
        self.open_issues: dict[str, list[Issue]] = {}

    def file(self, ticket: LiveTicket, operation: Operation) -> tuple[str, dict[str, Any] | None] | None:
        """Files the issues and returns `filed`, or `failed` with the artifact that says why.

        It returns None while it waits to try the Tracker again.
        """
        assert ticket.workspace is not None
        before = self.git.head(ticket.workspace, ticket.branch)
        count, retry_at = self.failures.get(ticket.id, (0, self.ports.clock.now()))
        if self.ports.clock.now() < retry_at:
            return None
        self.open_issues.pop(ticket.id, None)
        try:
            filed, after = self._file(ticket, operation, before)
        except Refused as refusal:
            return self._failed(ticket, operation, before, refusal)
        except TrackerError as error:
            if count + 1 >= TRIES:
                return self._failed(ticket, operation, before, Refused(f"the Tracker failed {TRIES} times: {error}"))
            self.failures[ticket.id] = (count + 1, self.ports.clock.now() + timedelta(seconds=RETRY_SECONDS))
            return None
        except (GitError, OSError) as error:
            return self._failed(ticket, operation, before, Refused(str(error)))
        self.failures.pop(ticket.id, None)
        self._finished(ticket, operation, before, "filed", after, issues=filed)
        return "filed", None

    def _file(self, ticket: LiveTicket, operation: Operation, before: str) -> tuple[list[dict[str, Any]], str]:
        assert ticket.workspace is not None
        entries = self._entries(ticket, operation)
        placeholders = self._placeholders(ticket)
        for key, files in placeholders.items():
            if key not in entries:
                raise Refused(
                    f"{files[0]} has the placeholder #new:{key}, which no issue of the Stages lists", key, files[0]
                )
        numbers = {key: self._issue(ticket, operation, entry) for key, entry in entries.items()}
        filed = [{"key": key, "number": number, "reused": reused} for key, (number, reused) in numbers.items()]
        try:
            self._replace(ticket, {key: number for key, (number, _) in numbers.items()})
            left = self._placeholders(ticket)
            if left:
                key, files = next(iter(left.items()))
                raise Refused(f"{files[0]} still has the placeholder #new:{key}", key, files[0])
            trailers = {
                "Factory-Run": self.context.run_id,
                "Factory-Ticket": ticket.id,
                "Factory-Stage": operation.name,
            }
            subject = f"Link the issues filed by {operation.name}: " + ", ".join(f"#{i['number']}" for i in filed)
            after = self.git.commit_all(ticket.workspace, ticket.branch, subject, trailers) or before
        except (Refused, GitError, OSError):
            # A failed Operation leaves the Workspace as the Stage committed it, without half the numbers put in.
            self.git.reset(ticket.workspace, ticket.branch, ignored=False)
            raise
        return filed, after

    def _entries(self, ticket: LiveTicket, operation: Operation) -> dict[str, dict[str, Any]]:
        """Reads the `issues` of the latest output of each Stage the Operation reads, keyed by their key.

        A key listed twice fails the Operation, because a placeholder could mean either issue.
        """
        entries: dict[str, dict[str, Any]] = {}
        for name in operation.from_:
            latest = [a for a in ticket.artifacts if a["kind"] == "stage" and a["node"] == name][-1:]
            for artifact in latest:
                output = _read_json(Path(artifact["path"]))
                for entry in output.get("issues", []):
                    if entry["key"] in entries:
                        raise Refused(
                            f"the key {entry['key']} is used twice in the issues of {name}",
                            entry["key"],
                            artifact["path"],
                        )
                    entries[entry["key"]] = entry
        return entries

    def _placeholders(self, ticket: LiveTicket) -> dict[str, list[str]]:
        """Returns the files of the Workspace that hold a placeholder in their text or their name, by key."""
        assert ticket.workspace is not None
        found: dict[str, list[str]] = {}
        for name in self.git.files(ticket.workspace, ticket.branch):
            path = ticket.workspace / name
            text = path.read_bytes() if path.is_file() and not path.is_symlink() else b""
            for key in dict.fromkeys(m.group(1).decode() for m in PLACEHOLDER.finditer(name.encode() + b"\0" + text)):
                found.setdefault(key, []).append(name)
        return found

    def _issue(self, ticket: LiveTicket, operation: Operation, entry: dict[str, Any]) -> tuple[int, bool]:
        """Returns the number of the issue behind the entry and whether it was reused, filing it when there is none.

        The journal knows an issue filed before a crash, and the marker in the body finds one the crash left
        unjournaled.
        """
        key = entry["key"]
        for done in ticket.issues:
            if done["node"] == operation.name and done["key"] == key:
                return done["number"], done["reused"]
        tracker = self.context.tracker
        marker = f"<!-- factory:run={self.context.run_id} ticket={ticket.id} key={key} -->"
        title = tracker.defuse(str(entry["title"])).strip()
        open_issues = self._open_issues(ticket, operation)
        number = _reusable(open_issues, marker, title)
        reused = number is not None
        if number is None:
            labels = list(dict.fromkeys([*operation.labels, *entry.get("labels", [])]))
            body = f"{tracker.defuse(str(entry['body'])).strip()}\n\n{marker}"
            number = tracker.create_issue(title, body, labels, operation.milestone)
            open_issues.append(Issue(number, title, body))
        self.append(
            Event.IssueFiled,
            {
                "ticket": ticket.id,
                "node": operation.name,
                "key": key,
                "number": number,
                "title": title,
                "reused": reused,
            },
        )
        return number, reused

    def _open_issues(self, ticket: LiveTicket, operation: Operation) -> list[Issue]:
        """Returns the open issues with the Operation's labels, read once per try and grown by the issues it files."""
        if ticket.id not in self.open_issues:
            self.open_issues[ticket.id] = self.context.tracker.find_issues(operation.labels)
        return self.open_issues[ticket.id]

    def _replace(self, ticket: LiveTicket, numbers: dict[str, int]) -> None:
        assert ticket.workspace is not None
        for files in self._placeholders(ticket).values():
            for name in files:
                path = ticket.workspace / name
                if not path.is_file() or path.is_symlink():
                    continue
                path.write_bytes(PLACEHOLDER.sub(lambda m: b"#%d" % numbers[m.group(1).decode()], path.read_bytes()))

    def _failed(
        self, ticket: LiveTicket, operation: Operation, before: str, refusal: Refused
    ) -> tuple[str, dict[str, Any] | None]:
        self.failures.pop(ticket.id, None)
        assert ticket.workspace is not None
        failures = sum(a["kind"] == "operation" and a["node"] == operation.name for a in ticket.artifacts)
        path = self.context.run_dir.ticket_dir(ticket.id) / operation.name / f"failed-{failures + 1}.json"
        content = {"reason": refusal.reason, "key": refusal.key, "file": refusal.file}
        write_json_once(path, content)
        artifact = {"kind": "operation", "node": operation.name, "path": str(path)}
        self._finished(ticket, operation, before, "failed", before, reason=refusal.reason, artifact=artifact)
        return "failed", artifact

    def _finished(
        self, ticket: LiveTicket, operation: Operation, before: str, outcome: str, after: str, **extra: Any
    ) -> None:
        payload = {"ticket": ticket.id, "node": operation.name, "kind": operation.kind, "before": before}
        duration = ticket.seconds_in_node(self.ports.clock.now())
        self.append(
            Event.OperationFinished, {**payload, "outcome": outcome, "after": after, "duration": duration, **extra}
        )


def _reusable(issues: list[Issue], marker: str, title: str) -> int | None:
    """Returns the number of the issue that carries the marker, or else the first one with the title."""
    for issue in issues:
        if marker in issue.body:
            return issue.number
    for issue in issues:
        if issue.title.strip() == title:
            return issue.number
    return None


def _read_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data
