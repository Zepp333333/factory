"""The CI wait is the Run node `wait_ci`, which waits for the CI of the Run's PR and ends in how the CI ended, and the
Decision node `ci_failed` of a Run graph, which puts red CI to the owner.

The Run node `wait_tracker` waits beside it, for an event on the PR or on the Spec."""

from collections.abc import Callable
from dataclasses import asdict, replace
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
from factory.journal.run_state import CI_ENDS, CiWait, RunMemory, TrackerWait
from factory.pipeline.pipeline import RETRY, Decision, RunNode
from factory.ports.run_ports import Ports
from factory.ports.tracker import (
    Discussions,
    FailedJob,
    PullRequests,
    TicketBoard,
    TrackerError,
    TrackerEvent,
    TrackerEvents,
)
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
# A Run with no CI to wait for has none, and neither has one whose red CI the owner accepted, as in `ACCEPTABLE`.
WAIT_CI_OUTCOMES = {"passed": "green", "failed": "red", "timeout": "timeout"}
ACCEPTABLE = ("failed", "timeout")

# The Run reads the CI of its PR this often, in seconds.
CI_POLL_SECONDS = 30

# The outcome of a `wait_tracker` node that heard no event in time, or had no PR to wait on.
TIMED_OUT = "timeout"
# What a failed read of the events calls its action in the Journal.
WAIT_TRACKER_READ = "events"


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
        return "absent" if memory.ci_ended in ACCEPTABLE else WAIT_CI_OUTCOMES.get(memory.ci_ended, "absent")

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
        if wait is None and self.context.memory.ci_ended in ACCEPTABLE:
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


class TrackerWaiter:
    """TrackerWaiter waits in a `wait_tracker` node for an event on the PR or on the Spec.

    It polls at the project's `tracker_poll` while the Run beats and takes the owner's Commands, and holds no slot,
    because no agent works.
    `discussions` and `board` are what `factory check` makes sure a Run graph that waits on the Spec has.
    """

    def __init__(
        self,
        context: RunContext,
        journal: Journal,
        append: Append,
        project: Project,
        ports: Ports,
        view: Display,
        pull_requests: PullRequests,
        discussions: Discussions | None,
        board: TicketBoard | None,
        beat: Callable[[], None],
        inbox: Inbox,
    ) -> None:
        self.context = context
        self.journal = journal
        self.append = append
        self.project = project
        self.ports = ports
        self.view = view
        self.pull_requests = pull_requests
        self.discussions = discussions
        self.board = board
        self.beat = beat
        self.apply_commands = inbox.apply

    def wait(self, node: RunNode) -> str:
        """Waits for one of the events of the node, or its `timeout`, and returns the node's outcome.

        A Run that resumes goes on with the wait of its Journal, so its `timeout` counts from when the wait began, and
        it reads the Tracker before it looks at the time, so an event that came while it was orphaned still counts.
        A Run with no PR to wait on, because it opened none or keeps it as a file, times out at once.
        """
        source, target = self._source(node)
        if target is None:
            self.view.warn(f"{node.name}: no PR to wait on")
            return TIMED_OUT
        polled: datetime | None = None
        while (wait := self.context.memory.tracker_wait) is None or not wait.outcome:
            self.beat()
            self.view.prompt(self.journal, self.context.run_id)
            self.apply_commands()
            now = self.ports.clock.now()
            if polled is None or (now - polled).total_seconds() >= self.project.tracker_poll:
                polled = now
                self._poll(node, source, target)
                self.view.show(self.journal, self.context.run_id)
            else:
                self.view.tick(self.journal, self.context.run_id)
                self.ports.clock.sleep(HEARTBEAT_SECONDS)
        return wait.outcome

    def _source(self, node: RunNode) -> tuple[TrackerEvents, str | None]:
        """Returns where the node hears its events, and the target it hears them of, or None for no PR to wait on."""
        if node.waits_on_spec:
            # `factory check` and the start of `factory run` refuse this node on a Tracker without these roles.
            assert self.discussions is not None and self.board is not None
            return self.discussions, self.board.spec_id()
        pull_request = self.context.memory.pull_request
        return self.pull_requests, pull_request if self.pull_requests.pushes and pull_request else None

    def _poll(self, node: RunNode, source: TrackerEvents, target: str) -> None:
        """Reads the events once and ends the wait on the latest that is new, or when it ran out of time.

        A wait that starts on the Spec takes what the Spec holds as handled, since that came before this wait, where a
        PR is the Run's own and all that happened on it counts, also while the Run waited for its CI.
        A Tracker that fails spends nothing and cannot time the wait out, because it may hold the event.
        """
        memory = self.context.memory
        try:
            events = source.events(target)
        except (TrackerError, OSError) as error:
            self.append(
                Event.TrackerActionFailed,
                {
                    "node": node.name,
                    "target": target,
                    "action": WAIT_TRACKER_READ,
                    "on_fail": RETRY,
                    "reason": str(error),
                },
            )
            self.view.warn(f"{node.name}: reading the events of {target} failed: {error}")
            return
        if memory.tracker_wait is None:
            first = node.waits_on_spec and target not in memory.tracker_handled
            self.append(
                Event.TrackerWaitStarted,
                {
                    "node": node.name,
                    "target": target,
                    "at": self.ports.clock.now().isoformat(),
                    "handled": [event.id for event in events] if first else [],
                },
            )
        wait = memory.tracker_wait
        assert wait is not None
        handled = memory.tracker_handled.get(target, set())
        new = [event for event in events if event.kind in node.settings.on and event.id not in handled]
        elapsed = (self.ports.clock.now() - wait.started).total_seconds()
        if new:
            self._end(node, target, new[-1].kind, new[-1], [event.id for event in new])
        elif elapsed >= node.settings.timeout:
            self._end(node, target, TIMED_OUT, None, [])

    def _end(self, node: RunNode, target: str, outcome: str, event: TrackerEvent | None, handled: list[str]) -> None:
        """Journals how the wait ended, with every event it heard as handled, so a later wait does not hear them.

        An approval older than a request for changes it heard does not come back after the fix.
        """
        heard = {"event": asdict(event)} if event else {}
        self.append(
            Event.TrackerWaitEnded,
            {"node": node.name, "target": target, "outcome": outcome, "handled": handled, **heard},
        )


# What the events of the CI wait and of the wait for the Tracker tell the readers of the Journal.


@applies(Event.TrackerWaitStarted)
def _apply_tracker_wait_started(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.tracker_wait = TrackerWait(datetime.fromisoformat(payload["at"]))
    memory.tracker_handled.setdefault(payload["target"], set()).update(payload["handled"])


@applies(Event.TrackerWaitEnded)
def _apply_tracker_wait_ended(memory: RunMemory, payload: dict[str, Any]) -> None:
    if memory.tracker_wait is not None:
        memory.tracker_wait = replace(memory.tracker_wait, outcome=payload["outcome"])
    memory.tracker_handled.setdefault(payload["target"], set()).update(payload["handled"])


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
