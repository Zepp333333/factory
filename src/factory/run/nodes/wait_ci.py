"""The kind `wait_ci` waits for the CI of the Run's PR and ends in how the CI ended."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from factory.core.events import Event, applies
from factory.journal.artifacts import write_once
from factory.journal.question import render_failed_logs
from factory.journal.run_state import CI_ENDS, CiWait, RunMemory
from factory.pipeline.pipeline import WAIT_CI, RunNode
from factory.pipeline.run_kinds import WAIT_SHAPE, NodeRunner, RunNodeKind, fixed
from factory.ports.tracker import FailedJob, PullRequests
from factory.run.node_tools import NodeTools

# The outcome of the `wait_ci` node by how the CI ended.
# A Run with no CI to wait for has none, and neither has one whose red CI the owner accepted, as in `ACCEPTABLE`.
WAIT_CI_OUTCOMES = {"passed": "green", "failed": "red", "timeout": "timeout"}
ACCEPTABLE = ("failed", "timeout")

# The Run reads the CI of its PR this often, in seconds.
CI_POLL_SECONDS = 30


def ci_payload(outcome: str, commit: str, jobs: list[FailedJob], log: Path | None = None) -> dict[str, Any]:
    """Returns the payload of `ci_finished`: how the CI ended, on which commit, its failed jobs and its log."""
    failed = [{"name": job.name, "url": job.url} for job in jobs]
    return {"outcome": outcome, "commit": commit, "jobs": failed, **({"artifact": str(log)} if log else {})}


class WaitCiRunner(NodeRunner):
    """WaitCiRunner waits for the CI of a Run's PR, records how it ended, and takes the owner's Commands meanwhile."""

    def __init__(self, node: RunNode, tools: NodeTools) -> None:
        self.tools = tools
        self.context = tools.context
        self.append = tools.run_tools.append
        self.clock = tools.run_tools.ports.clock
        self.pull_requests = tools.run_tools.pull_requests
        self.ci = tools.run_tools.project.ci

    def step(self) -> str:
        self._wait()
        return self.outcome

    def finish(self) -> None:
        """Ends the wait of a Run the owner finishes with its PR open, which the Journal records as unchecked CI."""
        wait = self.context.memory.ci
        if wait is not None:
            self.append(Event.CiFinished, ci_payload("unchecked", wait.commit, []))

    @property
    def outcome(self) -> str:
        """Returns the outcome of the `wait_ci` node, once the wait returned."""
        memory = self.context.memory
        if memory.ci is not None:
            return WAIT_CI_OUTCOMES[memory.ci.outcome]
        return "absent" if memory.ci_ended in ACCEPTABLE else WAIT_CI_OUTCOMES.get(memory.ci_ended, "absent")

    def _wait(self) -> None:
        """Waits for the CI of the Run's final PR and records how it ended, unless the Run has no CI to wait for.

        A Run whose PullRequests push nothing has no CI, and a CI that is `absent` ends the wait at once.

        It polls at a fixed interval while the Run beats and takes the owner's Commands.
        A PR with no check on its head commit within `grace` takes the project as one without CI, and a PR whose head
        is still not the awaited commit once `grace` passed ends the wait `timeout` at once, with both commits.
        The wait ends on red CI or a CI that does not end within `timeout`, and the Run graph decides what follows.
        A wait that follows a pushed fix waits for the new head, one that follows anything else on red CI for the PR's
        current head, and a Run that resumes in this node goes on waiting for the commit it waited for.
        A Run from before the Decision was a node, whose owner accepted its red CI on the way here, waits no more.
        """
        pull_requests, ci = self.pull_requests, self.ci
        if ci is None or not pull_requests.pushes or not self.context.memory.pull_request:
            return
        wait = self.context.memory.ci
        if wait is None and self.context.memory.ci_ended in ACCEPTABLE:
            return
        if wait is None:
            self._start_ci(pull_requests, self.context.memory.pushed)
        else:
            self._start_ci(pull_requests, None if wait.failed else wait.commit)
        self.tools.watch(self._ended, self._poll_ci, CI_POLL_SECONDS)

    def _ended(self) -> bool:
        wait = self.context.memory.ci
        return wait is None or wait.failed

    def _start_ci(self, pull_requests: PullRequests, commit: str | None) -> None:
        """Journals that the Run waits for the CI of `commit`, which is the PR's head now when none is given."""
        commit = commit or pull_requests.head(self.context.memory.pull_request)
        self.append(
            Event.CiStarted,
            {"commit": commit, "url": self.context.memory.pull_request, "at": self.clock.now().isoformat()},
        )

    def _poll_ci(self) -> None:
        """Reads the CI once and ends the wait when it passed, failed, never showed up or ran out of time."""
        wait, ci = self.context.memory.ci, self.ci
        assert wait is not None and not wait.failed and ci is not None, "`watch` polls only while the CI wait runs"
        status = self.pull_requests.ci(self.context.memory.pull_request, wait.commit)
        elapsed = (self.clock.now() - wait.started).total_seconds()
        match status.state:
            case "passed":
                self.append(Event.CiFinished, ci_payload("passed", wait.commit, []))
            case "failed":
                self._red("failed", wait.commit, status.jobs)
            case "absent":
                self.append(Event.CiAbsent, {"commit": wait.commit})
            case "none" if elapsed >= ci.grace:
                self.append(Event.CiAbsent, {"commit": wait.commit, "grace": ci.grace})
            case _ if elapsed >= ci.grace and (head := self._head()) != wait.commit:
                self._red("timeout", wait.commit, status.jobs, head)
            case _ if elapsed >= ci.timeout:
                self._red("timeout", wait.commit, status.jobs)

    def _head(self) -> str:
        return self.pull_requests.head(self.context.memory.pull_request)

    def _red(self, outcome: str, commit: str, jobs: list[FailedJob], head: str | None = None) -> None:
        """Journals the red CI with its failed logs as an Artifact, and the `head` the PR is at when it is not
        `commit`."""
        log = None
        if jobs:
            number = self.context.memory.ci_waits
            log = write_once(self.context.run_dir.root / "ci" / f"failed-{number}.md", render_failed_logs(jobs))
        self.append(Event.CiFinished, {**ci_payload(outcome, commit, jobs, log), **({"head": head} if head else {})})


KIND = RunNodeKind(
    name=WAIT_CI, outcomes=fixed("green", "red", "absent", "timeout"), shape=WAIT_SHAPE, make=WaitCiRunner
)


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
        memory.ci_commit = payload["commit"] if ended else memory.ci_commit


@applies(Event.CiAbsent)
def _apply_ci_absent(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.ci, memory.ci_ended, memory.ci_commit = None, "absent", payload["commit"]
