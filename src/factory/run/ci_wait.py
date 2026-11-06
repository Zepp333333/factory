"""The CI wait is the Run node `wait_ci`, which waits for the CI of the Run's PR and ends in how the CI ended, and the
Decision node `ci_failed` of a Run graph, which puts red CI to the owner."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from factory.config import Ci, Project
from factory.core.events import Event, applies
from factory.journal.artifacts import write_once
from factory.journal.commands import RUN_LEVEL
from factory.journal.custody import HEARTBEAT_SECONDS
from factory.journal.journal import Journal
from factory.journal.question import render_ci_card, render_failed_logs
from factory.journal.run_state import CI_ENDS, CiWait, RunMemory
from factory.pipeline.pipeline import Decision
from factory.ports.run_ports import Ports
from factory.ports.tracker import FailedJob, PullRequests
from factory.run.inbox import Inbox
from factory.run.run_context import TICK_SECONDS, Append, Display, RunContext

# How the CI wait of a Run ended, as the Run's report tells it, by the `ci_ended` of its state.
CI_NOTES = {
    "passed": "CI passed",
    "absent": "no CI checks",
    "unchecked": "CI not checked",
    "failed": "CI failed, accepted by the owner",
    "timeout": "CI timed out, accepted by the owner",
}

# How a CI wait the Run graph ended with no answer of the owner ended, by its `outcome`.
UNACCEPTED_NOTES = {"failed": "CI failed", "timeout": "CI timed out"}

# The outcome of the `wait_ci` node by how the CI ended.
# A Run with no CI to wait for has none, and neither has one whose red CI the owner accepted, as in `ACCEPTED`.
WAIT_CI_OUTCOMES = {"passed": "green", "failed": "red", "timeout": "timeout"}
ACCEPTED = ("failed", "timeout")

# The Run reads the CI of its PR this often, in seconds.
CI_POLL_SECONDS = 30


def ci_payload(outcome: str, commit: str, jobs: list[FailedJob], log: Path | None = None) -> dict[str, Any]:
    """Returns the payload of `ci_finished`: how the CI ended, on which commit, its failed jobs and its log."""
    failed = [{"name": job.name, "url": job.url} for job in jobs]
    return {"outcome": outcome, "commit": commit, "jobs": failed, **({"artifact": str(log)} if log else {})}


class CiWaiter:
    """CiWaiter waits for the CI of a Run's PR and records how it ended, and takes the owner's Commands meanwhile."""

    def __init__(
        self,
        context: RunContext,
        journal: Journal,
        append: Append,
        project: Project,
        ports: Ports,
        view: Display,
        pull_requests: PullRequests,
        beat: Callable[[], None],
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

    @property
    def outcome(self) -> str:
        """Returns the outcome of the `wait_ci` node, once `wait` returned."""
        memory = self.context.memory
        if memory.ci is not None:
            return WAIT_CI_OUTCOMES[memory.ci.outcome]
        return "absent" if memory.ci_ended in ACCEPTED else WAIT_CI_OUTCOMES.get(memory.ci_ended, "absent")

    @property
    def note(self) -> str:
        """Returns how the CI of the Run ended as the Run's report tells it, also for red CI the Run graph left red."""
        wait = self.context.memory.ci
        return UNACCEPTED_NOTES.get(wait.outcome, "") if wait else CI_NOTES.get(self.context.memory.ci_ended, "")

    def wait(self) -> None:
        """Waits for the CI of the Run's final PR and records how it ended, unless the Run has no CI to wait for.

        A Run whose PullRequests push nothing has no CI, and a CI that is `absent` ends the wait at once.

        It polls at a fixed interval while the Run beats, and holds no slot, because no agent works.
        A PR with no check on its head commit within `grace` takes the project as one without CI.
        The wait ends on red CI or a CI that does not end within `timeout`, and the Run graph decides what follows.
        A wait that follows a pushed fix waits for the new head, one that follows anything else on red CI for the PR's
        current head, and a Run that resumes in this node goes on waiting for the commit it waited for.
        A Run from before the Decision was a node, whose owner accepted its red CI on the way here, waits no more.
        """
        pull_requests = self.pull_requests
        ci = self.project.ci
        if ci is None or not pull_requests.pushes or not self.context.memory.pull_request:
            return
        wait = self.context.memory.ci
        if wait is None and self.context.memory.ci_ended in ACCEPTED:
            return
        if wait is None:
            self._start_ci(pull_requests, self.context.memory.pushed)
        else:
            self._start_ci(pull_requests, None if wait.failed else wait.commit)
        polled: datetime | None = None
        while (wait := self.context.memory.ci) is not None and not wait.failed:
            self.beat()
            self.view.prompt(self.journal, self.context.run_id)
            self.apply_commands()
            now = self.ports.clock.now()
            if polled is None or (now - polled).total_seconds() >= CI_POLL_SECONDS:
                polled = now
                self._poll_ci(pull_requests, wait, ci)
                self.view.show(self.journal, self.context.run_id)
            else:
                self.view.tick(self.journal, self.context.run_id)
                self.ports.clock.sleep(HEARTBEAT_SECONDS)

    def ask(self, node: Decision) -> str:
        """Opens the Decision of the Run graph unless it is open, and returns the owner's answer.

        The wait holds no slot, the Run keeps beating, and a Run that resumes in this node goes on with what it asked.
        """
        memory = self.context.memory
        if memory.run_decided is None and memory.run_waiting is None:
            self._open(node)
        while (decided := self.context.memory.run_decided) is None:
            self.beat()
            self.view.prompt(self.journal, self.context.run_id)
            if not self.apply_commands():
                self.view.tick(self.journal, self.context.run_id)
                self.ports.clock.sleep(self.tick)
        return decided

    def _start_ci(self, pull_requests: PullRequests, commit: str | None) -> None:
        """Journals that the Run waits for the CI of `commit`, which is the PR's head now when none is given."""
        commit = commit or pull_requests.head(self.context.memory.pull_request)
        self.append(
            Event.CiStarted,
            {"commit": commit, "url": self.context.memory.pull_request, "at": self.ports.clock.now().isoformat()},
        )

    def _poll_ci(self, pull_requests: PullRequests, wait: CiWait, ci: Ci) -> None:
        """Reads the CI once and ends the wait when it passed, failed, never showed up or ran out of time."""
        status = pull_requests.ci(self.context.memory.pull_request, wait.commit)
        elapsed = (self.ports.clock.now() - wait.started).total_seconds()
        match status.state:
            case "passed":
                self.append(Event.CiFinished, ci_payload("passed", wait.commit, []))
            case "failed":
                self._red("failed", wait.commit, status.jobs)
            case "absent":
                self.append(Event.CiAbsent, {"commit": wait.commit})
            case "none" if elapsed >= ci.grace:
                self.append(Event.CiAbsent, {"commit": wait.commit, "grace": ci.grace})
            case _ if elapsed >= ci.timeout:
                self._red("timeout", wait.commit, status.jobs)

    def _red(self, outcome: str, commit: str, jobs: list[FailedJob]) -> None:
        """Journals the red CI with its failed logs as an Artifact."""
        log = None
        if jobs:
            number = self.context.memory.ci_waits
            log = write_once(self.context.run_dir.root / "ci" / f"failed-{number}.md", render_failed_logs(jobs))
        self.append(Event.CiFinished, ci_payload(outcome, commit, jobs, log))

    def _open(self, node: Decision) -> None:
        """Opens the Decision with a card of the red CI and of the jobs of every Round of the CI fixer."""
        memory = self.context.memory
        number = memory.run_asked + 1
        wait = memory.ci
        outcome, commit = (wait.outcome, wait.commit) if wait else ("", memory.pushed)
        jobs = wait.jobs if wait else ()
        names = [f"Round {fix['round']}: {job['name']}: {job['url']}" for fix in memory.ci_fixes for job in fix["jobs"]]
        if not memory.ci_fixes or memory.ci_fixes[-1]["commit"] != commit:
            names += [f"{job['name']}: {job['url']}" for job in jobs]
        log = Path(wait.artifact) if wait and wait.artifact else None
        offered = {name: node.descriptions.get(name, "") for name in node.declared}
        card = write_once(
            self.context.run_dir.root / "decisions" / f"{node.name}-{number}.md",
            render_ci_card(self.context.run_id, node.name, outcome, commit, names, log, offered),
        )
        self.append(
            Event.DecisionWaiting,
            {
                "ticket": RUN_LEVEL,
                "node": node.name,
                "round": number,
                "card": str(card),
                "outcomes": list(offered),
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
    memory.ci_waits += 1


@applies(Event.CiFinished)
def _apply_ci_finished(memory: RunMemory, payload: dict[str, Any]) -> None:
    if memory.ci is not None:
        ended = payload["outcome"] in CI_ENDS
        failed = replace(
            memory.ci, outcome=payload["outcome"], jobs=tuple(payload["jobs"]), artifact=payload.get("artifact", "")
        )
        memory.ci = None if ended else failed
        memory.ci_ended = payload["outcome"] if ended else memory.ci_ended


@applies(Event.CiAbsent)
def _apply_ci_absent(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.ci, memory.ci_ended = None, "absent"
