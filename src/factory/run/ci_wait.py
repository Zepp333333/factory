"""The CI wait is the last phase of a Run: it waits for the CI of the Run's PR, lets the CI fixer work on red CI
and opens the Decision `ci_failed` once the fixer has no Round left."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from factory.config import Ci, Project
from factory.core.events import Event, applies
from factory.journal.commands import RUN_LEVEL
from factory.journal.custody import HEARTBEAT_SECONDS
from factory.journal.journal import Journal
from factory.journal.question import render_ci_card, render_failed_logs
from factory.journal.run_state import CI_DECISION, CI_ENDS, CI_OUTCOMES, CiWait, RunMemory
from factory.ports.tracker import FailedJob, PullRequestFolder, PullRequests
from factory.run.ci_fix import CiFixer
from factory.run.inbox import Inbox
from factory.run.run_context import TICK_SECONDS, Append, Ports, RunContext
from factory.views.artifacts import write_once
from factory.views.run_view import RunView

# How the CI wait of a Run ended, as the Run's report tells it, by the `ci_ended` of its state.
CI_NOTES = {
    "passed": "CI passed",
    "absent": "no CI checks",
    "unchecked": "CI not checked",
    "failed": "CI failed, accepted by the owner",
    "timeout": "CI timed out, accepted by the owner",
}

# The Run reads the CI of its PR this often, in seconds.
CI_POLL_SECONDS = 30


def ci_payload(outcome: str, commit: str, jobs: list[FailedJob], log: Path | None = None) -> dict[str, Any]:
    """Returns the payload of `ci_finished`: how the CI ended, on which commit, its failed jobs and its log."""
    failed = [{"name": job.name, "url": job.url} for job in jobs]
    return {"outcome": outcome, "commit": commit, "jobs": failed, **({"artifact": str(log)} if log else {})}


class CiWaiter:
    """CiWaiter waits for the CI of a Run's PR and records how it ended, and takes the owner's Commands meanwhile.

    `fixer` works the Rounds of the CI fix, and is None when the Run has no PR on a forge to fix.
    """

    def __init__(
        self,
        context: RunContext,
        journal: Journal,
        append: Append,
        project: Project,
        ports: Ports,
        view: RunView,
        pull_requests: PullRequests | PullRequestFolder,
        beat: Callable[[], None],
        fixer: CiFixer | None,
        inbox: Inbox,
    ) -> None:
        self.journal = journal
        self.append = append
        self.project = project
        self.ports = ports
        self.view = view
        self.pull_requests = pull_requests
        self.beat = beat
        self.apply_commands = inbox.apply
        self.tick = TICK_SECONDS
        self.context = context
        self.fixer = fixer

    def wait(self) -> None:
        """Waits for the CI of the Run's final PR and records how it ended, unless the Run has no CI to wait for.

        It polls at a fixed interval while the Run beats, and holds no slot, because no agent works.
        A PR with no check on its head commit within `grace` takes the project as one without CI.
        Red CI first goes to the CI fixer when the Pipeline has one, a Round at a time, and a Round that pushes a fix
        makes the Run wait for the CI of the new head.
        Red CI after the last Round, or CI that does not end within `timeout`, opens the Decision `ci_failed`,
        and the wait goes on until the owner answers it:
        `recheck` waits for the PR's current head, and `accept` ends the Run.
        A Run that resumes in this phase goes on waiting for the commit it waited for.
        """
        pull_requests = self.pull_requests
        ci = self.project.ci
        if ci is None or isinstance(pull_requests, PullRequestFolder) or not self.context.memory.pull_request:
            return
        wait = self.context.memory.ci
        if wait is None or not wait.failed:
            self._start_ci(pull_requests, wait.commit if wait else self.context.memory.pushed)
        polled: datetime | None = None
        while self.context.memory.ci is not None:
            self.beat()
            self.view.prompt(self.journal, self.context.run_id)
            moved = self.apply_commands()
            if (wait := self.context.memory.ci) is None:
                break
            if wait.failed:
                if self.context.memory.run_waiting is None:
                    self._start_ci(pull_requests, None)
                    polled = None
                elif not moved:
                    self.view.tick(self.journal, self.context.run_id)
                    self.ports.clock.sleep(self.tick)
                continue
            now = self.ports.clock.now()
            if polled is None or (now - polled).total_seconds() >= CI_POLL_SECONDS:
                polled = now
                self._poll_ci(pull_requests, wait, ci)
                self.view.show(self.journal, self.context.run_id)
            else:
                self.view.tick(self.journal, self.context.run_id)
                self.ports.clock.sleep(HEARTBEAT_SECONDS)

    def _start_ci(self, pull_requests: PullRequests, commit: str | None) -> None:
        """Journals that the Run waits for the CI of `commit`, which is the PR's head now when none is given."""
        commit = commit or pull_requests.head(self.context.memory.pull_request)
        self.append(
            Event.CiStarted,
            {"commit": commit, "url": self.context.memory.pull_request, "at": self.ports.clock.now().isoformat()},
        )

    def _red(self, pull_requests: PullRequests, commit: str, jobs: list[FailedJob]) -> None:
        """Lets the CI fixer take Rounds on red CI until one pushes a fix, and opens the Decision when none is left."""
        assert self.fixer is not None
        pushed = None
        while pushed is None and self.fixer.rounds_left:
            pushed = self.fixer.fix(commit, jobs)
        if pushed is None:
            self._ci_failed("failed", commit, jobs)
        else:
            self._start_ci(pull_requests, pushed)

    def _poll_ci(self, pull_requests: PullRequests, wait: CiWait, ci: Ci) -> None:
        """Reads the CI once and ends the wait when it passed, failed, never showed up or ran out of time."""
        status = pull_requests.ci(self.context.memory.pull_request, wait.commit)
        elapsed = (self.ports.clock.now() - wait.started).total_seconds()
        match status.state:
            case "passed":
                self.append(Event.CiFinished, ci_payload("passed", wait.commit, []))
            case "failed":
                self._red(pull_requests, wait.commit, status.jobs)
            case "none" if elapsed >= ci.grace:
                self.append(Event.CiAbsent, {"commit": wait.commit, "grace": ci.grace})
            case _ if elapsed >= ci.timeout:
                self._ci_failed("timeout", wait.commit, status.jobs)

    def _ci_failed(self, outcome: str, commit: str, jobs: list[FailedJob]) -> None:
        """Journals the red CI with its failed logs as an Artifact, and opens the Decision `ci_failed`."""
        number = self.context.memory.run_asked + 1
        log = None
        if jobs:
            log = write_once(self.context.run_dir.root / "ci" / f"failed-{number}.md", render_failed_logs(jobs))
        self.append(Event.CiFinished, ci_payload(outcome, commit, jobs, log))
        names = [
            f"Round {fix['round']}: {job['name']}: {job['url']}"
            for fix in self.context.memory.ci_fixes
            for job in fix["jobs"]
        ]
        fixes = self.context.memory.ci_fixes
        if not fixes or fixes[-1]["commit"] != commit:
            names += [f"{job.name}: {job.url}" for job in jobs]
        card = write_once(
            self.context.run_dir.root / "decisions" / f"{CI_DECISION}-{number}.md",
            render_ci_card(self.context.run_id, CI_DECISION, outcome, commit, names, log, CI_OUTCOMES),
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


# What the events of the CI wait tell the readers of the Journal.


@applies(Event.CiStarted)
def _apply_ci_started(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.ci = CiWait(payload["commit"], datetime.fromisoformat(payload["at"]))


@applies(Event.CiFinished)
def _apply_ci_finished(memory: RunMemory, payload: dict[str, Any]) -> None:
    if memory.ci is not None:
        ended = payload["outcome"] in CI_ENDS
        memory.ci = None if ended else replace(memory.ci, outcome=payload["outcome"])
        memory.ci_ended = payload["outcome"] if ended else memory.ci_ended


@applies(Event.CiAbsent)
def _apply_ci_absent(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.ci, memory.ci_ended = None, "absent"
