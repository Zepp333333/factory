"""The CI wait is the last phase of a Run: it waits for the CI of the Run's PR and opens the Decision `ci_failed`."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from factory.artifacts import RunDir, write_once
from factory.attempt import Ports
from factory.commands import RUN_LEVEL
from factory.config import Ci, Project
from factory.custody import HEARTBEAT_SECONDS
from factory.events import Event
from factory.journal import Journal
from factory.question import render_ci_card, render_failed_logs
from factory.run_state import CI_DECISION, CiWait, RunMemory
from factory.run_view import RunView
from factory.tracker import FailedJob, PullRequestFolder, PullRequests

# The Decision `ci_failed` offers these outcomes.
CI_OUTCOMES = {
    "recheck": "Wait for the CI of the PR's current head again, after you fixed or reran something",
    "accept": "End the Run as it is, with the red CI recorded",
}

# The Run reads the CI of its PR this often, in seconds.
CI_POLL_SECONDS = 30


def ci_payload(outcome: str, commit: str, jobs: list[FailedJob], log: Path | None = None) -> dict[str, Any]:
    failed = [{"name": job.name, "url": job.url} for job in jobs]
    return {"outcome": outcome, "commit": commit, "jobs": failed, **({"artifact": str(log)} if log else {})}


class Scope(Protocol):
    """A Scope is the Run that waits: its id, its directory, the state its events tell and the note its wait sets."""

    run_id: str
    run_dir: RunDir
    memory: RunMemory
    pushed: str
    ci_note: str


class CiWaiter:
    """CiWaiter waits for the CI of a Run's PR and records how it ended.

    `append` journals an event and applies it to the Run state, `beat` keeps the Run's heartbeat, and `apply_commands`
    takes the owner's Commands in the inbox and returns whether any came.
    """

    def __init__(
        self,
        journal: Journal,
        append: Callable[[Event, dict[str, Any]], None],
        project: Project,
        ports: Ports,
        view: RunView,
        pull_requests: PullRequests | PullRequestFolder,
        beat: Callable[[], None],
        apply_commands: Callable[[], bool],
        tick: float,
        scope: Scope,
    ) -> None:
        self.journal = journal
        self.append = append
        self.project = project
        self.ports = ports
        self.view = view
        self.pull_requests = pull_requests
        self.beat = beat
        self.apply_commands = apply_commands
        self.tick = tick
        self.scope = scope

    def wait(self) -> None:
        """Waits for the CI of the Run's final PR and records how it ended, unless the Run has no CI to wait for.

        It polls at a fixed interval while the Run beats, and holds no slot, because no agent works.
        A PR with no check on its head commit within `grace` takes the project as one without CI.
        Red CI, or CI that does not end within `timeout`, opens the Decision `ci_failed`, and the wait goes on
        until the owner answers it: `recheck` waits for the PR's current head, and `accept` ends the Run.
        A Run that resumes in this phase goes on waiting for the commit it waited for.
        """
        pull_requests = self.pull_requests
        ci = self.project.ci
        if ci is None or isinstance(pull_requests, PullRequestFolder) or not self.scope.memory.pull_request:
            return
        wait = self.scope.memory.ci
        if wait is None or not wait.failed:
            self._start_ci(pull_requests, wait.commit if wait else self.scope.pushed)
        polled: datetime | None = None
        while self.scope.memory.ci is not None:
            self.beat()
            self.view.prompt(self.journal, self.scope.run_id)
            moved = self.apply_commands()
            if (wait := self.scope.memory.ci) is None:
                break
            if wait.failed:
                if self.scope.memory.run_waiting is None:
                    self._start_ci(pull_requests, None)
                    polled = None
                elif not moved:
                    self.view.tick(self.journal, self.scope.run_id)
                    self.ports.clock.sleep(self.tick)
                continue
            now = self.ports.clock.now()
            if polled is None or (now - polled).total_seconds() >= CI_POLL_SECONDS:
                polled = now
                self._poll_ci(pull_requests, wait, ci)
                self.view.show(self.journal, self.scope.run_id)
            else:
                self.view.tick(self.journal, self.scope.run_id)
                self.ports.clock.sleep(HEARTBEAT_SECONDS)

    def _start_ci(self, pull_requests: PullRequests, commit: str | None) -> None:
        """Journals that the Run waits for the CI of `commit`, which is the PR's head now when none is given."""
        commit = commit or pull_requests.head(self.scope.memory.pull_request)
        self.append(
            Event.CiStarted,
            {"commit": commit, "url": self.scope.memory.pull_request, "at": self.ports.clock.now().isoformat()},
        )

    def _poll_ci(self, pull_requests: PullRequests, wait: CiWait, ci: Ci) -> None:
        """Reads the CI once and ends the wait when it passed, failed, never showed up or ran out of time."""
        status = pull_requests.ci(self.scope.memory.pull_request, wait.commit)
        elapsed = (self.ports.clock.now() - wait.started).total_seconds()
        match status.state:
            case "passed":
                self.append(Event.CiFinished, ci_payload("passed", wait.commit, []))
                self.scope.ci_note = "CI passed"
            case "failed":
                self._ci_failed("failed", wait.commit, status.jobs)
            case "none" if elapsed >= ci.grace:
                self.append(Event.CiAbsent, {"commit": wait.commit, "grace": ci.grace})
                self.scope.ci_note = "no CI checks"
            case _ if elapsed >= ci.timeout:
                self._ci_failed("timeout", wait.commit, status.jobs)

    def _ci_failed(self, outcome: str, commit: str, jobs: list[FailedJob]) -> None:
        """Journals the red CI with its failed logs as an Artifact, and opens the Decision `ci_failed`."""
        number = self.scope.memory.run_asked + 1
        log = None
        if jobs:
            log = write_once(self.scope.run_dir.root / "ci" / f"failed-{number}.md", render_failed_logs(jobs))
        self.append(Event.CiFinished, ci_payload(outcome, commit, jobs, log))
        names = [f"{job.name}: {job.url}" for job in jobs]
        card = write_once(
            self.scope.run_dir.root / "decisions" / f"{CI_DECISION}-{number}.md",
            render_ci_card(self.scope.run_id, CI_DECISION, outcome, commit, names, log, CI_OUTCOMES),
        )
        self.append(
            Event.DecisionWaiting,
            {
                "ticket": RUN_LEVEL,
                "node": CI_DECISION,
                "round": number,
                "card": str(card),
                "outcomes": list(CI_OUTCOMES),
                "cause": f"ci_{outcome}",
                "tickets": [],
                "gave_back": [],
            },
        )
        self.view.ring()
