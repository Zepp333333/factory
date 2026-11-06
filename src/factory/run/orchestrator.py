"""The Orchestrator is the deterministic loop that drives one Run and is the only writer of its events."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from factory.core.events import Event, applies, measures, nothing, times
from factory.core.states import RUN_TERMINAL_NODES, RunState, TicketState
from factory.journal.metrics_memory import MetricsMemory
from factory.journal.projection import Projection
from factory.journal.run_state import RunMemory
from factory.journal.timeline_memory import TimelineMemory, duration
from factory.pipeline.pipeline import (
    OPEN_PR,
    TICKETS,
    TRACKER_OPERATIONS,
    WAIT_CI,
    Decision,
    Gate,
    Operation,
    RunNode,
    RunStage,
    Stage,
)
from factory.pipeline.throttle import Braked, Throttle
from factory.run.ci_fix import CiFixer
from factory.run.ci_wait import CI_NOTES, CiWaiter, ci_payload
from factory.run.decision import Decisions
from factory.run.inbox import Inbox
from factory.run.landing import Lander
from factory.run.live import Ended, LiveTicket
from factory.run.moves import Moves
from factory.run.operations import RUN_NODE_ACTS, Operations, TrackerInputs, TrackerOperations
from factory.run.probes import Probes
from factory.run.run_context import TICK_SECONDS, RunContext
from factory.run.run_start import RunStart
from factory.run.run_tools import RunTools

# A delivery to the Tracker that failed is tried again after this many seconds.
TRACKER_RETRY_SECONDS = 30

# The reason a Run ends with when the owner answers its Decision with `abort`.
OWNER_ABORTED = "the owner aborted the Run after Attempts failed in a row"


class Orchestrator:
    """The Orchestrator drives the Run of its context with the tools of the Run.

    Only `append` of the tools changes the Run's state, and the parts that do the work of a phase read the same context.
    The Orchestrator is the only phase that takes the tools whole: it hands each phase the tools that phase uses.
    """

    def __init__(self, context: RunContext, tools: RunTools) -> None:
        self.context = context
        self.tools = tools
        self.journal = tools.journal
        self.project = tools.project
        self.pipeline = tools.pipeline
        self.ports = tools.ports
        self.view = tools.view
        self.custody = tools.custody
        self.window = tools.window
        self.attempts = tools.attempts
        self.throttle = Throttle()
        self.by_hand: list[list[str]] = []
        # Each phase is built here once from the tools it uses, so one Run holds one instance of each.
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
        self.probes = Probes(context, tools.append, tools.ports, tools.git, tools.attempts.isolation, self.moves)
        self.lander = Lander(context, tools.append, tools.project, tools.git, tools.pull_requests, tools.run_agents)
        fixer = CiFixer(
            context,
            tools.append,
            tools.project,
            tools.ports,
            tools.git,
            tools.pull_requests,
            tools.beat,
            tools.run_agents,
        )
        self.ci_wait = CiWaiter(
            context,
            tools.journal,
            tools.append,
            tools.project,
            tools.ports,
            tools.view,
            tools.pull_requests,
            tools.beat,
            fixer,
            self.inbox,
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
        self.retry_delivery_at: datetime | None = None
        # What each kind of Run node does with its node, which returns the node's outcome, or None for a Run the owner
        # aborted.
        self.run_nodes: dict[str, Callable[[RunNode], str | None]] = {
            TICKETS: lambda _: self._run_tickets(),
            OPEN_PR: lambda _: self._open_pr(),
            WAIT_CI: lambda _: self._wait_ci(),
            **dict.fromkeys(TRACKER_OPERATIONS, self._act_on_tracker),
        }

    @property
    def run_id(self) -> str:
        return self.context.run_id

    @property
    def memory(self) -> RunMemory:
        return self.context.memory

    @property
    def tickets(self) -> list[LiveTicket]:
        return self.context.tickets

    @property
    def started(self) -> bool:
        """Tells whether this process began the Run or took it up, so that it has a Run to end."""
        return self.custody.began

    @property
    def pull_request(self) -> str:
        return self.memory.pull_request

    @property
    def ci_note(self) -> str:
        return CI_NOTES.get(self.memory.ci_ended, "")

    @property
    def paused_until(self) -> datetime | None:
        return self.memory.paused_until

    @property
    def braked(self) -> dict[str, Braked]:
        return self.memory.braked

    @property
    def run_waiting(self) -> tuple[str, int] | None:
        return self.memory.run_waiting

    @property
    def max_parallel(self) -> int:
        return self.memory.max_parallel

    def start(self) -> None:
        """Starts the Run on the snapshot the Tracker gave, and lands it through the PullRequests the Run was given."""
        self.run_start.begin()

    def resume(self) -> None:
        """Takes up an orphaned Run from its events and puts its Tickets back where the journal says they were."""
        self.run_start.resume()

    def finish(self) -> RunState:
        """Ends an orphaned Run as `partial` with what it merged, and lands that.

        A Run orphaned in phase `ci` has landed already: it ends without waiting for the CI, which the journal records
        as unchecked, and its state is that of any Run whose Tickets all merged.
        No session of an unfinished Ticket comes back: each Ticket that did not merge is abandoned,
        with its unsaved work kept under its ref, and the Run lands like any Run that ends `partial`.
        """
        state = self.run_start.reclaim()
        for ticket in self.tickets:
            if ticket.state in (TicketState.QUEUED, TicketState.ACTIVE):
                self.run_start.give_up(ticket, state.in_flight.get(ticket.id), ticket.id in state.probing)
        self._deliver(last=True)
        graph = self.project.run_graph
        assert graph is not None
        if graph.run_nodes_of(OPEN_PR):
            if graph.run_stage is not None:
                self.lander.describe(graph.run_stage)
            self.by_hand = self.lander.open_pr()
        if self.memory.ci is not None:
            # The PR is open and the owner ends the Run without waiting for its CI.
            self.tools.append(Event.CiFinished, ci_payload("unchecked", self.memory.ci.commit, []))
            self._finish(self._final_state())
            return self._final_state()
        self._finish(RunState.PARTIAL)
        return RunState.PARTIAL

    def run(self) -> RunState:
        """Walks the Run graph from the node the Journal ends in, or its start, and returns the Run's final state.

        A Run that resumes goes on in the node it was in, and a Journal without Run node events starts at the start of
        the default Run graph, whose nodes do again only what the Journal does not hold yet.
        """
        graph = self.project.run_graph
        assert graph is not None
        self.view.show(self.journal, self.run_id)
        if self.memory.run_node or self.memory.run_next:
            # A later node does not tick, so the Tracker gets here what a delivery before the kill failed to hand.
            self._deliver(last=True)
        while (name := self.memory.run_node or self.memory.run_next or graph.start) not in RUN_TERMINAL_NODES:
            node = graph.nodes[name]
            assert isinstance(node, RunNode | RunStage), "Config proves that a Run graph holds only Run nodes"
            if self.memory.run_node is None:
                self.tools.append(Event.RunNodeEntered, {"node": name, "kind": node.kind})
                self.view.show(self.journal, self.run_id)
            outcome = self.lander.describe(node) if isinstance(node, RunStage) else self.run_nodes[node.kind](node)
            if outcome is None:
                return RunState.ABORTED
            self.tools.append(
                Event.RunNodeEnded, {"node": name, "outcome": outcome, "next": graph.route(node, outcome)}
            )
        state = self._final_state()
        self._finish(state)
        return state

    def _act_on_tracker(self, node: RunNode) -> str:
        """Acts on the targets of the node, and beats and takes the owner's Commands while it waits to try again."""
        while (outcome := self.tracker_operations.act_for_run(node)) is None:
            self.tools.beat()
            self.view.prompt(self.journal, self.run_id)
            self.inbox.apply()
            self.view.tick(self.journal, self.run_id)
            self.ports.clock.sleep(TICK_SECONDS)
        return outcome

    def _open_pr(self) -> str:
        self.by_hand = self.lander.open_pr()
        return self.lander.outcome

    def _wait_ci(self) -> str:
        self.ci_wait.wait()
        return self.ci_wait.outcome

    def _run_tickets(self) -> str | None:
        """Ticks until no Ticket can move and returns how many merged, or None for a Run the owner aborted."""
        while True:
            moved = self._tick()
            if self.memory.aborted:
                self.abort(OWNER_ABORTED)
                return None
            if moved:
                self.view.show(self.journal, self.run_id)
                continue
            self.view.tick(self.journal, self.run_id)
            if (
                self.attempts.starved
                or self.run_waiting
                or self.operations.waiting
                or self.tracker_inputs.waiting
                or any(t.busy or t.waiting for t in self.tickets)
            ):
                self.ports.clock.sleep(TICK_SECONDS)
            elif waits := [t for t in (self.paused_until, *(b.until for b in self.braked.values())) if t]:
                until_reset = (min(waits) - self.ports.clock.now()).total_seconds()
                self.ports.clock.sleep(min(max(until_reset, TICK_SECONDS), self.custody.interval))
            else:
                break
        self._deliver(last=True)
        if not any(t.state == TicketState.MERGED for t in self.tickets):
            return "none_merged"
        return "all_merged" if self._final_state() == RunState.DONE else "some_merged"

    def _final_state(self) -> RunState:
        finished = all(t.state in (TicketState.MERGED, TicketState.DONE) for t in self.tickets)
        return RunState.DONE if finished else RunState.PARTIAL

    def abort(self, reason: str) -> None:
        """Ends a started Run as `aborted` after an error or a stop the Orchestrator cannot carry on from.

        It kills the agents and Probes still running, so nothing works on for a Run that has ended.
        """
        if self._stop_run_work():
            self._finish(RunState.ABORTED, reason)

    def interrupt(self, reason: str) -> None:
        """Leaves a started Run the owner stopped, with Ctrl-C or `kill`, orphaned at once and its slots freed.

        The journal then looks as if the process had died, so `resume` continues the Run and `finish` ends it.
        """
        if self._stop_run_work():
            self.custody.release(self.run_id, reason)

    def _stop_run_work(self) -> bool:
        """Kills the agents and Probes still running and returns whether this Orchestrator still owns the Run."""
        if not self.started:
            return False
        taken_over = not self.custody.holds(self.run_id)
        for ticket in self.tickets:
            if ticket.running is not None:
                self.attempts.kill(ticket.running)
            if ticket.probe is not None:
                ticket.probe.process.kill()
        return not taken_over

    def _finish(self, state: RunState, reason: str | None = None) -> None:
        self.tools.append(Event.RunFinished, {"state": state, **({"reason": reason} if reason else {})})
        self.view.show(self.journal, self.run_id)

    def _tick(self) -> bool:
        self.tools.beat()
        moved = self._resume()
        moved |= self._brake()
        self.view.prompt(self.journal, self.run_id)
        moved |= self.inbox.apply()
        moved |= self._budget()
        self._deliver()
        moved |= self.run_start.reconcile()
        self.attempts.starved = False
        for ticket in self.tickets:
            if ticket.state == TicketState.ACTIVE:
                self.moves.mark_stage(ticket)
                moved |= self._advance(ticket)
        return moved | self._claim_next()

    def _deliver(self, last: bool = False) -> None:
        """Hands the Tracker's TicketBoard the events after its cursor, and on failure journals why and waits before
        trying again.

        The `last` delivery at the end of the Run tries once whatever the wait, and a failure never stops the Run.
        A Tracker without a TicketBoard gets no events.
        """
        now = self.ports.clock.now()
        board = self.tools.board
        if board is None or (not last and self.retry_delivery_at is not None and now < self.retry_delivery_at):
            return
        failure = Projection(self.journal, self.run_id, board, self.tools.append).deliver()
        if failure is None:
            self.retry_delivery_at = None
            return
        self.retry_delivery_at = now + timedelta(seconds=TRACKER_RETRY_SECONDS)
        self.tools.append(Event.TrackerDeliveryFailed, failure)

    def _held(self, executor: str | None = None) -> bool:
        """Tells whether new Attempts on `executor` wait, for a rate limit to reset, for the brake of the Executor to
        let go, for the owner to answer the Run's Decision or raise a spent budget, or for the Run to end after the
        owner aborted it.

        Without an Executor it tells whether new Attempts on no Executor wait, as the claim of a Ticket that starts at
        a Gate or an Operation, which the brake holds back once every Executor of the Run is braked.
        """
        braked = executor in self.braked if executor else self.braked.keys() >= self.ports.executors.keys()
        spent = bool(self.memory.over_budget())
        return self.paused_until is not None or braked or spent or self.run_waiting is not None or self.memory.aborted

    def _resume(self) -> bool:
        """Ends the pause once its reset time comes, and returns whether it did."""
        if self.paused_until is None or self.ports.clock.now() < self.paused_until:
            return False
        self.tools.append(Event.RunResumed, {"resets_at": self.paused_until.isoformat()})
        return True

    def _brake(self) -> bool:
        """Turns the brake of each Executor on when one of its windows is used beyond its `cap` or `pace`, and off when
        they are within bounds again, and returns whether it changed any.

        A brake that stays on is journaled again only when another window holds the Executor back or its time has
        passed, so the Journal does not log every change of usage.
        A brake on an Executor the Run does not have, as a bare window of an old Journal, goes off.
        """
        moved = False
        for name in dict.fromkeys([*self.ports.executors, *self.braked]):
            moved |= self._brake_executor(name)
        return moved

    def _brake_executor(self, name: str) -> bool:
        executor, now = self.ports.executors.get(name), self.ports.clock.now()
        windows = {f"{name}.{window}": usage for window, usage in executor.windows().items()} if executor else {}
        found = self.throttle.check(self.memory.throttle, windows, now)
        current = self.braked.get(name)
        if found is None:
            if current is None:
                return False
            usage = windows.get(current.window)
            reset = usage is None or now >= usage.resets_at
            payload = {"window": current.window, "used": None if usage is None or reset else usage.used}
            self.tools.append(Event.ThrottleOff, {**payload, "reason": "window reset" if reset else "within bounds"})
            return True
        if current is None or current.window != found.window or now >= current.until:
            self.tools.append(Event.ThrottleOn, found.payload())
            return True
        return False

    def _budget(self) -> bool:
        """Abandons what did not merge of a Run the owner finished at its budget, and otherwise lets the Run's Decisions
        weigh the budget, and returns whether either moved anything."""
        if self.memory.finishing:
            return self.inbox.abandon_unfinished()
        return self.tools.run_decisions.weigh_budget(self._attempt_waits())

    def _attempt_waits(self) -> bool:
        """Tells whether a Ticket would start an Attempt now, at a node with an agent or on the Frontier, if it could.

        A Ticket at a Gate or an Operation goes on, so the Run asks about a budget only once one gets to an agent.
        """
        merged = {t.id for t in self.tickets if t.state == TicketState.MERGED}
        for ticket in self.tickets:
            if ticket.state == TicketState.QUEUED and set(ticket.blocked_by) <= merged:
                return True
            if ticket.state == TicketState.ACTIVE and ticket.running is None and ticket.node is not None:
                node = self.pipeline.nodes[ticket.node]
                if isinstance(node, Stage) or (isinstance(node, Decision) and node.decider.role is not None):
                    return True
        return False

    def _claim_next(self) -> bool:
        """Claims the Frontier's queued Tickets in order, up to `max_parallel` active ones, and returns whether it did.

        A Ticket is on the Frontier once all its blockers are merged, so one that depends on an abandoned Ticket
        never starts.
        Claiming stops at the first Ticket that finds no free slot, and waits out a pause like any new Attempt.
        A Ticket whose Tracker inputs cannot be read waits for its next try, and the Tickets behind it go on.
        """
        merged = {t.id for t in self.tickets if t.state == TicketState.MERGED}
        claimed = False
        # The claim starts the first Attempt of a Pipeline that starts at a Stage, on that Stage's Executor.
        start = self.pipeline.nodes[self.pipeline.start]
        executor = start.settings.executor if isinstance(start, Stage) else None
        for ticket in self.tickets:
            active = sum(1 for t in self.tickets if t.state == TicketState.ACTIVE)
            if active >= self.max_parallel or self._held(executor):
                break
            if ticket.state == TicketState.QUEUED and set(ticket.blocked_by) <= merged:
                if not self.tracker_inputs.read(ticket, start):
                    continue
                if not self.moves.claim(ticket):
                    break
                claimed = True
        return claimed

    def _advance(self, ticket: LiveTicket) -> bool:
        assert ticket.node is not None
        node = self.pipeline.nodes[ticket.node]
        if isinstance(node, Stage):
            ended = self._step_attempt(ticket, node)
            if not isinstance(ended, Ended):
                return ended
            outcome, artifact = ended.outcome, ended.artifact
            assert outcome is not None
        elif isinstance(node, Decision):
            decided = self.decisions.decide(ticket, node, lambda: self._step_attempt(ticket, node))
            if not isinstance(decided, tuple):
                return decided
            outcome, artifact = decided
        elif isinstance(node, Gate):
            probed = self.probes.step(ticket, node)
            if not isinstance(probed, tuple):
                return probed
            outcome, artifact = probed
        elif isinstance(node, Operation):
            if node.takes_window and not self.window.hold(ticket.id, node.name):
                return False
            operated = self.operations.operate(ticket, node)
            if operated is None:
                return False
            outcome, artifact = operated
        else:
            raise AssertionError(f"Config refuses {node.name}, which this engine cannot run yet")
        self.moves.leave(ticket, node, outcome, artifact)
        return True

    def _step_attempt(self, ticket: LiveTicket, node: Stage | Decision) -> Ended | bool:
        """Starts or steps the Attempt of a node, and returns whether the Ticket moved, or how the node ended.

        A failed Attempt with Attempts left in the Round is replaced on the next tick, or after the pause.
        """
        if ticket.running is None:
            # Only Attempts wait out a pause, because Probes and Operations do not use the subscription.
            if self._held(node.agent.settings.executor) or not self.tracker_inputs.read(ticket, node):
                return False
            return self.attempts.start(ticket, node)
        ended = self.attempts.step(ticket, node, lambda: self.run_start.reconcile_ticket(ticket))
        if ended is None:
            return False
        return ended if ended.outcome is not None else True


# What the events the Orchestrator writes tell the readers of the Journal.

applies(Event.RunFinished, Event.TrackerDeliveryFailed)(nothing)


@measures(Event.RunNodeEntered)
def _measure_run_node_entered(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    memory.enter_run_node(payload["node"])


@measures(Event.RunNodeEnded)
def _measure_run_node_ended(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    memory.leave_run_node(memory.at, payload["outcome"])


@applies(Event.RunNodeEntered)
def _apply_run_node_entered(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.run_node, memory.run_next = payload["node"], None
    memory.acted.pop(RUN_NODE_ACTS, None)


@applies(Event.RunNodeEnded)
def _apply_run_node_ended(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.run_node, memory.run_next = None, payload["next"]


@applies(Event.ThrottleOn)
def _apply_throttle_on(memory: RunMemory, payload: dict[str, Any]) -> None:
    braked = Braked.of(payload)
    memory.braked[braked.executor] = braked


@applies(Event.ThrottleOff)
def _apply_throttle_off(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.braked.pop(payload["window"].partition(".")[0], None)


@applies(Event.RunResumed)
def _apply_run_resumed(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.paused_until = None


@times(Event.RunFinished)
def _time_run_finished(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    memory.close_all()
    took = duration((memory.at - memory.started).total_seconds()) if memory.started else "-"
    memory.line("run", payload["state"], f"{took}  ${memory.total_cost:.2f}")


@measures(Event.RunResumed)
def _measure_run_resumed(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if memory.paused_since is not None:
        memory.pauses.append((memory.paused_since, memory.at))
        memory.paused_since = None


@measures(Event.RunFinished)
def _measure_run_finished(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    memory.ended, memory.state = memory.at, str(payload.get("state"))
