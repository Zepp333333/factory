"""The kind `wait_tracker` waits for an event on the PR or the Spec, and ends in that event or in `timeout`."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any

from factory.core import values
from factory.core.events import Event, applies
from factory.journal.run_state import RunMemory, TrackerWait
from factory.pipeline.pipeline import (
    OPEN_PR,
    PR_TARGET,
    RETRY,
    SPEC_TARGET,
    WAIT_TRACKER,
    RunNode,
)
from factory.pipeline.run_kinds import WAIT_SHAPE, NodeRunner, Order, Report, RunNodeKind, names, parsed, with_block
from factory.ports.tracker import DISCUSSIONS, PR_EVENTS, TICKET_BOARD, TrackerError, TrackerEvent, TrackerEvents
from factory.run.node_tools import NodeTools

# What a `wait_tracker` node waits for, on the PR or on the Spec, where only a PR gets a review.
WAIT_TRACKER_TARGETS = (PR_TARGET, SPEC_TARGET)
REVIEW_EVENTS = ("approved", "changes_requested")
TRACKER_EVENTS = (*REVIEW_EVENTS, "commented", "labeled")

# The outcome of a `wait_tracker` node that heard no event in time, or had no PR to wait on.
TIMED_OUT = "timeout"
# What a failed read of the events calls its action in the Journal.
WAIT_TRACKER_READ = "events"


@dataclass(frozen=True)
class WaitTracker:
    """WaitTracker is what a `wait_tracker` node waits for: the events `on` its target, until its `timeout`."""

    on: tuple[str, ...]
    timeout: float | None
    target: str | None


def parse(where: str, body: Mapping[str, Any], report: Report) -> WaitTracker:
    """Reads the `with` of a `wait_tracker`: the events `on` its target, by default the PR, and its `timeout`."""
    block = with_block(where, body, report, required=("on", "timeout"), optional=("target",))
    at, fields = block.where, block.fields
    target = parsed(f"{at}.target", fields.get("target", PR_TARGET), values.choice(WAIT_TRACKER_TARGETS), report)
    on = names(at, fields, "on", report)
    if "on" in fields and not on:
        report(f"{at}.on", "names no event to wait for")
    for event in on:
        if parsed(f"{at}.on", event, values.choice(TRACKER_EVENTS), report) in REVIEW_EVENTS and target == SPEC_TARGET:
            report(f"{at}.on", f"has {event}, which only a PR gets")
    timeout = parsed(f"{at}.timeout", fields["timeout"], values.duration, report) if "timeout" in fields else None
    return WaitTracker(on, timeout, target)


def after(node: Any) -> tuple[str, ...]:
    """Returns the kinds that have run on every path to the node, where a wait for an event on the PR needs the PR."""
    return (OPEN_PR,) if node.settings.target == PR_TARGET else ()


def roles(node: Any) -> tuple[str, ...]:
    """Returns the roles the node hears its events through: those of the code host for the PR, and for the Spec the
    Discussions, and the TicketBoard that names the Spec."""
    return (PR_EVENTS,) if node.settings.target == PR_TARGET else (DISCUSSIONS, TICKET_BOARD)


class WaitTrackerRunner(NodeRunner):
    """WaitTrackerRunner waits in a `wait_tracker` node for an event on the PR or on the Spec.

    It polls at the project's `tracker_poll` while the Run beats and takes the owner's Commands.
    `factory check` makes sure the project serves the roles the node hears its events through.
    """

    def __init__(self, node: RunNode, tools: NodeTools) -> None:
        run_tools = tools.run_tools
        self.node = node
        self.tools = tools
        self.context = tools.context
        self.append = run_tools.append
        self.project = run_tools.project
        self.clock = run_tools.ports.clock
        self.view = run_tools.view
        self.pull_requests = run_tools.pull_requests
        self.discussions = run_tools.discussions
        self.board = run_tools.board

    def step(self) -> str:
        """Waits for one of the events of the node, or its `timeout`, and returns the node's outcome.

        A Run that resumes goes on with the wait of its Journal, so its `timeout` counts from when the wait began, and
        it reads the Tracker before it looks at the time, so an event that came while it was orphaned still counts.
        A Run with no PR to wait on, because it opened none or its code host hears no events on a PR, times out at once.
        """
        node = self.node
        source, target = self._source(node)
        if source is None or target is None:
            self.view.warn(f"{node.name}: no PR to wait on")
            return TIMED_OUT
        self.tools.watch(self._ended, lambda: self._poll(node, source, target), self.project.tracker_poll)
        wait = self.context.memory.tracker_wait
        assert wait is not None
        return wait.outcome

    def _ended(self) -> bool:
        wait = self.context.memory.tracker_wait
        return wait is not None and bool(wait.outcome)

    def _source(self, node: RunNode) -> tuple[TrackerEvents | None, str | None]:
        """Returns where the node hears its events, and the target it hears them of, or None for no PR to wait on.

        A code host that hears no events on a PR, which `factory check` refuses, is no source, as for a Run resumed
        after its landing changed.
        """
        if node.waits_on_spec:
            # `factory check` and the start of `factory run` refuse this node on a Tracker without these roles.
            assert self.discussions is not None and self.board is not None
            return self.discussions, self.board.spec_id()
        return self.pull_requests.pr_events, self.context.memory.pull_request or None

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
                    "at": self.clock.now().isoformat(),
                    "handled": [event.id for event in events] if first else [],
                },
            )
        wait = memory.tracker_wait
        assert wait is not None
        handled = memory.tracker_handled.get(target, set())
        new = [event for event in events if event.kind in node.settings.on and event.id not in handled]
        elapsed = (self.clock.now() - wait.started).total_seconds()
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


KIND = RunNodeKind(
    name=WAIT_TRACKER,
    outcomes=lambda node: (*node.settings.on, TIMED_OUT),
    parse=parse,
    order=Order(after=after),
    roles=roles,
    holds_pr=True,
    shape=WAIT_SHAPE,
    make=WaitTrackerRunner,
)


# What the events of the wait for the Tracker tell the readers of the Journal.


@applies(Event.TrackerWaitStarted)
def _apply_tracker_wait_started(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.tracker_wait = TrackerWait(datetime.fromisoformat(payload["at"]))
    memory.tracker_handled.setdefault(payload["target"], set()).update(payload["handled"])


@applies(Event.TrackerWaitEnded)
def _apply_tracker_wait_ended(memory: RunMemory, payload: dict[str, Any]) -> None:
    if memory.tracker_wait is not None:
        memory.tracker_wait = replace(memory.tracker_wait, outcome=payload["outcome"])
    memory.tracker_handled.setdefault(payload["target"], set()).update(payload["handled"])
