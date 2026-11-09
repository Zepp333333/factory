"""The NodeTools are the services of one Run that more than one kind of Run node uses, built once per Run (ADR 0015).

The Orchestrator builds them from the RunTools and hands them to the `make` of each kind, so a kind's runner takes
what it uses from them, and builds a service that it uses alone, as `tickets` builds its Probes.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from factory.core.events import Event, applies, nothing
from factory.journal.custody import HEARTBEAT_SECONDS
from factory.journal.projection import Projection
from factory.pipeline.pipeline import Gate, Node, RunStage
from factory.pipeline.throttle import Throttle
from factory.run.ci_fix import CiFixer
from factory.run.decision import Decisions
from factory.run.inbox import Inbox
from factory.run.landing import Lander
from factory.run.landings import LANDINGS
from factory.run.moves import Moves
from factory.run.operations import TRACKER_RETRY_SECONDS, Operations, TrackerInputs, TrackerOperations
from factory.run.run_context import TICK_SECONDS, RunContext
from factory.run.run_start import RunStart
from factory.run.run_tools import RunTools


class NodeTools:
    """NodeTools hold the RunContext and the RunTools of a Run, and the services that its kinds of nodes share.

    The Ticket loop of `tickets`, the owner's Commands in the Inbox, the start and the finish of the Run all work on
    the Tickets, so `Moves`, `Decisions`, `Operations` and the Tracker's operations and inputs are built here once.
    The Throttle of the brake remembers the usage it first saw in each window, so it lives as long as the Run.
    The Landing of the project's `landing:` answers what depends on how the Run lands, the Lander writes the PR texts
    and opens the PRs it plans, and the CiFixer works the Rounds of the Run Stages that commit and their Run Gates,
    and lands the fix where the Landing says as the walk leaves them.
    """

    def __init__(self, context: RunContext, tools: RunTools) -> None:
        self.context = context
        self.run_tools = tools
        self.decisions = Decisions(context, tools.append, tools.pipeline, tools.ports, tools.git, tools.view.ring)
        self.tracker_operations = TrackerOperations(
            context, tools.append, tools.ports, tools.view, tools.board, tools.beat
        )
        self.tracker_inputs = TrackerInputs(context, tools.append, tools.ports, tools.view, tools.discussions)
        self.operations = Operations(
            context, tools.append, tools.ports, tools.git, tools.attempts.isolation, tools.desk, self.tracker_operations
        )
        self.moves = Moves(
            context,
            tools.journal,
            tools.append,
            tools.project,
            tools.ports,
            tools.home,
            tools.git,
            tools.window,
            tools.attempts,
            self.decisions,
            self.operations,
        )
        self.inbox = Inbox(
            context,
            tools.journal,
            tools.append,
            tools.pipeline,
            tools.git,
            tools.window,
            tools.attempts,
            tools.run_decisions,
            self.decisions,
            self.moves,
        )
        self.run_start = RunStart(
            context,
            tools.append,
            tools.project,
            tools.ports,
            tools.git,
            tools.attempts,
            tools.window,
            tools.custody,
            self.moves,
        )
        self.landing = LANDINGS[tools.project.landing](context, tools.git, tools.pull_requests, tools.project.base)
        self.lander = Lander(context, tools.append, self.landing, tools.git, tools.pull_requests, tools.run_agents)
        self.fixer = CiFixer(
            context,
            tools.append,
            tools.project,
            tools.ports,
            tools.git,
            tools.pull_requests,
            self.landing,
            tools.beat,
            tools.run_agents,
            self.read_run_inputs,
        )
        # The pace of the brake counts from the usage it first saw, so one Throttle serves the whole Run.
        self.throttle = Throttle()
        self.retry_delivery_at: datetime | None = None

    def hold(self, seconds: float) -> None:
        """Beats, shows the Run and takes the owner's Commands for `seconds`, while a node waits to try again."""
        tools = self.run_tools
        until = tools.ports.clock.now() + timedelta(seconds=seconds)
        while True:
            tools.beat()
            tools.view.prompt(tools.journal, self.context.run_id)
            self.inbox.apply()
            tools.view.tick(tools.journal, self.context.run_id)
            tools.ports.clock.sleep(TICK_SECONDS)
            if tools.ports.clock.now() >= until:
                return

    def settle(self, entering: Node | None) -> None:
        """Lands the fix of a Round once the walk leaves the Run Gates after its Stage, or the Stage when it has none,
        so the walk enters any node but a Run Gate with the fix landed.

        The Orchestrator calls it before each node it enters, and after `resume` too, so a fix lands once also when the
        Run was killed between its last Gate and the next node.
        """
        if not isinstance(entering, Gate):
            self.fixer.land()

    def retry(self) -> None:
        """Holds a node whose read or action on the Tracker failed until the Tracker may be tried again."""
        self.hold(TRACKER_RETRY_SECONDS)

    def watch(self, done: Callable[[], bool], poll: Callable[[], None], every: float) -> None:
        """Calls `poll` every `every` seconds until `done`, while the Run beats, shows and takes the owner's Commands.

        A node that watches holds no slot, because no agent works.
        """
        tools = self.run_tools
        polled: datetime | None = None
        while not done():
            tools.beat()
            tools.view.prompt(tools.journal, self.context.run_id)
            self.inbox.apply()
            now = tools.ports.clock.now()
            if polled is None or (now - polled).total_seconds() >= every:
                polled = now
                poll()
                tools.view.show(tools.journal, self.context.run_id)
            else:
                tools.view.tick(tools.journal, self.context.run_id)
                tools.ports.clock.sleep(HEARTBEAT_SECONDS)

    def read_run_inputs(self, stage: RunStage, handled: set[str]) -> dict[str, list[dict[str, Any]]]:
        """Reads the Tracker inputs of a Run Stage, and holds it while the Tracker fails, which spends nothing."""
        while (heard := self.tracker_inputs.read_for_run(stage, handled)) is None:
            self.retry()
        return heard

    def deliver(self, last: bool = False) -> None:
        """Hands the Tracker's TicketBoard the events after its cursor, and on failure journals why and waits before
        trying again.

        The `last` delivery at the end of the Run tries once whatever the wait, and a failure never stops the Run.
        A Tracker without a TicketBoard gets no events.
        """
        tools = self.run_tools
        now = tools.ports.clock.now()
        board = tools.board
        if board is None or (not last and self.retry_delivery_at is not None and now < self.retry_delivery_at):
            return
        failure = Projection(tools.journal, self.context.run_id, board, tools.append).deliver()
        if failure is None:
            self.retry_delivery_at = None
            return
        self.retry_delivery_at = now + timedelta(seconds=TRACKER_RETRY_SECONDS)
        tools.append(Event.TrackerDeliveryFailed, failure)


# What the events of the delivery tell the readers of the Journal.

applies(Event.TrackerDeliveryFailed)(nothing)
