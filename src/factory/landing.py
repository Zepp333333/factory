"""Landing is the end of a Run that merged a Ticket: the PR text, the push of the Integration branch and the PR.

The text is the Describer's when the Pipeline has one, and the Tracker's own otherwise.
`PullRequests` opens the PR, and a Run with no git remote to land through records it in a folder.
"""

import re
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from factory.artifacts import RunDir, write_once
from factory.attempt import Attempts, Ports, schema_errors
from factory.config import Project
from factory.events import Event
from factory.executor import AttemptResult
from factory.git import GitError, HostGit
from factory.live import LiveTicket
from factory.names import abandoned_ref
from factory.pipeline import Pipeline, Role
from factory.run_state import RunMemory
from factory.sandbox import Mount
from factory.states import TicketState
from factory.tracker import PullRequest, PullRequestFolder, PullRequests, SpecSnapshot, Tracker, TrackerError

# The PR body the Describer writes holds these sections, and `Merge Danger` holds both of these entries.
PR_SECTIONS = ("Summary", "Evidence", "Merge Danger")
PR_DANGER = ("Door", "Blast Radius")


def describer_names(home: Path, run_id: str) -> tuple[str, str, Path]:
    """Returns the Sandbox, branch and Workspace of the Describer, none of which a Ticket's can equal.

    Those of a Ticket start with the Run id or sit under its directory, and a Run id starts with a digit.
    """
    return (
        f"describer-{run_id}",
        f"factory/describer-{run_id}",
        home / "workspaces" / f"describer-{run_id}" / "workspace",
    )


def landing_remote(git: HostGit, pull_requests: PullRequests) -> str:
    """Returns the first git remote, by name, that points at the repository the PR opens in."""
    for name, url in sorted(git.remotes().items()):
        if pull_requests.accepts(url):
            return name
    raise GitError(f"no git remote of {git.repo} points at {pull_requests.url()}")


class Scope(Protocol):
    """A Scope is the Run that lands: its ids, its directory, the Spec and Tracker it works on and what landing sets."""

    run_id: str
    run_dir: RunDir
    integration: str
    base_commit: str
    snapshot: SpecSnapshot
    tracker: Tracker
    memory: RunMemory
    pull_request: str
    pushed: str
    by_hand: list[list[str]]

    @property
    def tickets(self) -> list[LiveTicket]: ...


class Lander:
    """Lander writes the PR text of a Run, pushes its Integration branch and opens its PR.

    `append` journals an event and applies it to the Run state.
    """

    def __init__(
        self,
        append: Callable[[Event, dict[str, Any]], None],
        project: Project,
        pipeline: Pipeline,
        ports: Ports,
        home: Path,
        git: HostGit,
        attempts: Attempts,
        pull_requests: PullRequests | PullRequestFolder,
        scope: Scope,
    ) -> None:
        self.append = append
        self.project = project
        self.pipeline = pipeline
        self.ports = ports
        self.home = home
        self.git = git
        self.attempts = attempts
        self.pull_requests = pull_requests
        self.scope = scope

    def land(self) -> None:
        """Writes the PR text of a Run that merged a Ticket, then pushes the Integration branch and opens its PR.

        The text is an Artifact in any case, and a Run with no git remote to land through records the PR in a folder.
        A Run that resumes after its PR opened lands nothing again.
        A step that fails is journaled with its reason and leaves the Run's state alone,
        and `by_hand` then holds the commands that finish the landing.
        """
        if self.scope.memory.pull_request or not any(t.state == TicketState.MERGED for t in self.scope.tickets):
            return
        states = {t.id: t.state for t in self.scope.tickets}
        kept = {t.id: abandoned_ref(self.scope.run_id, t.id) for t in self.scope.tickets if t.kept}
        pull = self._describe(states, kept)
        body = write_once(self.scope.run_dir.pull_request, pull.body)
        write_once(self.scope.run_dir.pull_request_title, f"{pull.title}\n")
        pull_requests = self.pull_requests
        if isinstance(pull_requests, PullRequestFolder):
            self.scope.pull_request = str(
                pull_requests.record(self.scope.integration, self.project.base, pull.title, body)
            )
            self.append(
                Event.PullRequestRecorded,
                {"path": self.scope.pull_request, "head": self.scope.integration, "base": self.project.base},
            )
            return
        open_command = pull_requests.open_command(self.scope.integration, self.project.base, pull.title, body)
        remote = None
        try:
            commit = self.git.resolve(self.scope.integration)
            self.scope.pushed = commit
            remote = landing_remote(self.git, pull_requests)
            self.git.push(remote, self.scope.integration)
        except GitError as error:
            self.append(Event.LandingFailed, {"step": "push", "reason": str(error)})
            self.scope.by_hand = [["git", "push", remote or pull_requests.url(), self.scope.integration], open_command]
            return
        self.append(
            Event.IntegrationBranchPushed, {"remote": remote, "branch": self.scope.integration, "commit": commit}
        )
        try:
            self.scope.pull_request = pull_requests.open(self.scope.integration, self.project.base, pull.title, body)
        except TrackerError as error:
            self.append(Event.LandingFailed, {"step": "pull_request", "reason": str(error)})
            self.scope.by_hand = [open_command]
            return
        self.append(
            Event.PullRequestOpened,
            {
                "url": self.scope.pull_request,
                "head": self.scope.integration,
                "base": self.project.base,
                "body": str(body),
            },
        )

    def _describe(self, states: dict[str, TicketState], kept: dict[str, str]) -> PullRequest:
        """Returns the PR text, which the Describer writes when the Pipeline has one.

        The engine appends its own footer to the Describer's body, so closing the Tickets does not depend on the agent.
        The Tracker defuses the closing keywords in that body, so only the footer closes issues.
        A Describer that fails every Attempt is journaled with the reason of each, and the Tracker's own text stands in.
        """
        fallback = self.scope.tracker.describe(self.scope.snapshot, self.scope.run_id, states, kept)
        describer = self.pipeline.describer
        if describer is None:
            return fallback
        names = describer_names(self.home, self.scope.run_id)
        for attempt in range(1, describer.attempts + 1):
            tip = self.git.resolve(self.scope.integration)
            result = self.attempts.describe(
                describer,
                attempt,
                names,
                tip,
                partial(self._describer_brief, attempt),
                self._describer_mounts,
            )
            problems = self._pull_request_problems(describer.role, result)
            if not problems:
                assert result.output is not None
                footer = self.scope.tracker.footer(self.scope.snapshot, states, kept)
                body = f"{self.scope.tracker.defuse(str(result.output['body'])).strip()}\n\n{footer}"
                self.append(Event.PullRequestDescribed, {"attempt": attempt, "title": result.output["title"]})
                return PullRequest(title=str(result.output["title"]), body=body)
            cause = result.failure or "invalid_output"
            silent = {"silent": result.silent} if result.silent is not None else {}
            self.append(Event.DescriberFailed, {"attempt": attempt, "cause": cause, "reason": problems, **silent})
        return fallback

    def _pull_request_problems(self, role: Role, result: AttemptResult) -> str:
        """Tells why the Describer's result is no PR text, or returns an empty text when it is one."""
        if result.failure is not None:
            return result.reason or result.failure
        errors = schema_errors(role.output, result.output)
        if errors or result.output is None:
            return "; ".join(errors)
        title = str(result.output["title"])
        if not title.strip() or title != title.strip() or "\n" in title or "\r" in title:
            return "the title must be one line with no space around it"
        body = str(result.output["body"])
        missing = [name for name in PR_SECTIONS if not re.search(rf"^#+\s+{name}\s*$", body, re.MULTILINE)]
        missing += [name for name in PR_DANGER if name not in body]
        return f"the body lacks {', '.join(missing)}" if missing else ""

    def _describer_mounts(self) -> list[Mount]:
        """Lists what the Describer's Sandbox sees besides its Workspace: the Run's Artifacts, read-only."""
        self.scope.run_dir.agent_home.mkdir(exist_ok=True)
        self.scope.run_dir.describer.mkdir(exist_ok=True)
        return [
            Mount(self.git.common),
            Mount(self.scope.run_dir.spec),
            Mount(self.scope.run_dir.root / "tickets"),
            Mount(self.scope.run_dir.describer, writable=True),
            Mount(self.scope.run_dir.agent_home, writable=True),
            Mount(self.scope.run_dir.kit),
            *(Mount(path) for path in self.ports.executor.reads()),
        ]

    def _describer_brief(self, attempt: int, attempt_dir: Path) -> dict[str, Any]:
        """Builds the Describer's Brief from the Spec, the merged Tickets with their Gate verdicts and Stage outputs,
        the base ref and the Integration branch.
        """
        inputs: list[dict[str, Any]] = [{"source": "spec", "path": str(self.scope.run_dir.spec)}]
        for ticket in self.scope.tickets:
            if ticket.state != TicketState.MERGED:
                continue
            inputs.append({"source": "ticket", "ticket": ticket.id, "path": str(self.scope.run_dir.ticket(ticket.id))})
            for artifact in ticket.artifacts:
                entry = {k: v for k, v in artifact.items() if k != "kind"}
                inputs.append({"source": artifact["kind"], "ticket": ticket.id, **entry})
        return {
            "run": self.scope.run_id,
            "stage": "describe",
            "attempt": attempt,
            "base": self.scope.base_commit,
            "base_ref": self.project.base,
            "integration_branch": self.scope.integration,
            "inputs": inputs,
            "output_dir": str(attempt_dir),
        }
