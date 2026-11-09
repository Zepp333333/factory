"""The kind `tickets` runs the Ticket Pipeline over the Tickets of the Run.

Its runner ticks: it claims the Frontier's Tickets, steps the node each active Ticket is at, holds new Attempts back
for a pause, the brake of an Executor or a spent budget, and ends once no Ticket can move.
"""

from collections.abc import Mapping
from typing import Any

from factory.core.events import Event, applies, measures
from factory.core.states import TicketState
from factory.journal.metrics_memory import MetricsMemory
from factory.journal.run_state import RunMemory
from factory.pipeline.pipeline import TICKETS, Decision, Gate, Operation, RunNode, Stage
from factory.pipeline.run_kinds import ONE, TICKETS_SHAPE, NodeRunner, Order, Report, RunNodeKind, fixed, with_block
from factory.pipeline.throttle import Braked
from factory.run.live import Ended, LiveTicket
from factory.run.node_tools import NodeTools
from factory.run.probes import Probes
from factory.run.run_context import TICK_SECONDS


def parse(where: str, body: Mapping[str, Any], report: Report) -> None:
    """Reads the `with` of a `tickets` node, whose `pipeline` the project file reads before the Run graph loads."""
    with_block(where, body, report, required=("pipeline",))


class TicketsRunner(NodeRunner):
    """TicketsRunner works the Tickets of the Run through the Ticket Pipeline until none can move.

    The Probes of the Gates are its own, and it takes the rest from the NodeTools,
    which the owner's Commands, the start and the finish of the Run share.
    """

    def __init__(self, node: RunNode, tools: NodeTools) -> None:
        run_tools = tools.run_tools
        self.tools = tools
        self.context = tools.context
        self.journal = run_tools.journal
        self.append = run_tools.append
        self.pipeline = run_tools.pipeline
        self.ports = run_tools.ports
        self.view = run_tools.view
        self.window = run_tools.window
        self.attempts = run_tools.attempts
        self.probes = Probes(
            tools.context, run_tools.append, run_tools.ports, run_tools.git, run_tools.attempts.isolation, tools.moves
        )
        self.throttle = tools.throttle

    @property
    def memory(self) -> RunMemory:
        return self.context.memory

    @property
    def tickets(self) -> list[LiveTicket]:
        return self.context.tickets

    def step(self) -> str | None:
        """Ticks until no Ticket can move and returns how many merged, or None for a Run the owner aborted."""
        while True:
            moved = self._tick()
            if self.memory.aborted:
                return None
            if moved:
                self.view.show(self.journal, self.context.run_id)
                continue
            self.view.tick(self.journal, self.context.run_id)
            if (
                self.attempts.starved
                or self.memory.run_waiting
                or self.tools.operations.waiting
                or self.tools.tracker_inputs.waiting
                or any(t.busy or t.waiting for t in self.tickets)
            ):
                self.ports.clock.sleep(TICK_SECONDS)
            elif waits := [t for t in (self.memory.paused_until, *(b.until for b in self.memory.braked.values())) if t]:
                until_reset = (min(waits) - self.ports.clock.now()).total_seconds()
                self.ports.clock.sleep(min(max(until_reset, TICK_SECONDS), self.tools.run_tools.custody.interval))
            else:
                break
        self.tools.deliver(last=True)
        if not any(t.state == TicketState.MERGED for t in self.tickets):
            return "none_merged"
        return "all_merged" if self.memory.finished else "some_merged"

    def _tick(self) -> bool:
        self.tools.run_tools.beat()
        moved = self._resume()
        moved |= self._brake()
        self.view.prompt(self.journal, self.context.run_id)
        moved |= self.tools.inbox.apply()
        moved |= self._budget()
        self.tools.deliver()
        moved |= self.tools.run_start.reconcile()
        self.attempts.starved = False
        for ticket in self.tickets:
            if ticket.state == TicketState.ACTIVE:
                self.tools.moves.mark_stage(ticket)
                moved |= self._advance(ticket)
        return moved | self._claim_next()

    def _held(self, executor: str | None = None) -> bool:
        """Tells whether new Attempts on `executor` wait, for a rate limit to reset, for the brake of the Executor to
        let go, for the owner to answer the Run's Decision or raise a spent budget, or for the Run to end after the
        owner aborted it.

        Without an Executor it tells whether new Attempts on no Executor wait, as the claim of a Ticket that starts at
        a Gate or an Operation, which the brake holds back once every Executor of the Run is braked.
        """
        memory = self.memory
        braked = executor in memory.braked if executor else memory.braked.keys() >= self.ports.executors.keys()
        spent = bool(memory.over_budget())
        return memory.paused_until is not None or braked or spent or memory.run_waiting is not None or memory.aborted

    def _resume(self) -> bool:
        """Ends the pause once its reset time comes, and returns whether it did."""
        paused_until = self.memory.paused_until
        if paused_until is None or self.ports.clock.now() < paused_until:
            return False
        self.append(Event.RunResumed, {"resets_at": paused_until.isoformat()})
        return True

    def _brake(self) -> bool:
        """Turns the brake of each Executor on when one of its windows is used beyond its `cap` or `pace`, and off when
        they are within bounds again, and returns whether it changed any.

        A brake that stays on is journaled again only when another window holds the Executor back or its time has
        passed, so the Journal does not log every change of usage.
        A brake on an Executor the Run does not have, as a bare window of an old Journal, goes off.
        """
        moved = False
        for name in dict.fromkeys([*self.ports.executors, *self.memory.braked]):
            moved |= self._brake_executor(name)
        return moved

    def _brake_executor(self, name: str) -> bool:
        executor, now = self.ports.executors.get(name), self.ports.clock.now()
        windows = {f"{name}.{window}": usage for window, usage in executor.windows().items()} if executor else {}
        found = self.throttle.check(self.memory.throttle, windows, now)
        current = self.memory.braked.get(name)
        if found is None:
            if current is None:
                return False
            usage = windows.get(current.window)
            reset = usage is None or now >= usage.resets_at
            payload = {"window": current.window, "used": None if usage is None or reset else usage.used}
            self.append(Event.ThrottleOff, {**payload, "reason": "window reset" if reset else "within bounds"})
            return True
        if current is None or current.window != found.window or now >= current.until:
            self.append(Event.ThrottleOn, found.payload())
            return True
        return False

    def _budget(self) -> bool:
        """Abandons what did not merge of a Run the owner finished at its budget, and otherwise lets the Run's Decisions
        weigh the budget, and returns whether either moved anything."""
        if self.memory.finishing:
            return self.tools.inbox.abandon_unfinished()
        return self.tools.run_tools.run_decisions.weigh_budget(self._attempt_waits())

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
            if active >= self.memory.max_parallel or self._held(executor):
                break
            if ticket.state == TicketState.QUEUED and set(ticket.blocked_by) <= merged:
                if not self.tools.tracker_inputs.read(ticket, start):
                    continue
                if not self.tools.moves.claim(ticket):
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
            decided = self.tools.decisions.decide(ticket, node, lambda: self._step_attempt(ticket, node))
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
            operated = self.tools.operations.operate(ticket, node)
            if operated is None:
                return False
            outcome, artifact = operated
        else:
            raise AssertionError(f"Config refuses {node.name}, which this engine cannot run yet")
        self.tools.moves.leave(ticket, node, outcome, artifact)
        return True

    def _step_attempt(self, ticket: LiveTicket, node: Stage | Decision) -> Ended | bool:
        """Starts or steps the Attempt of a node, and returns whether the Ticket moved, or how the node ended.

        A failed Attempt with Attempts left in the Round is replaced on the next tick, or after the pause.
        """
        if ticket.running is None:
            # Only Attempts wait out a pause, because Probes and Operations do not use the subscription.
            if self._held(node.agent.settings.executor) or not self.tools.tracker_inputs.read(ticket, node):
                return False
            return self.attempts.start(ticket, node)
        ended = self.attempts.step(ticket, node, lambda: self.tools.run_start.reconcile_ticket(ticket))
        if ended is None:
            return False
        return ended if ended.outcome is not None else True


KIND = RunNodeKind(
    name=TICKETS,
    outcomes=fixed("all_merged", "some_merged", "none_merged"),
    parse=parse,
    order=Order(count=ONE),
    shape=TICKETS_SHAPE,
    make=TicketsRunner,
)


# What the events of the pause and the brake tell the readers of the Journal.


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


@measures(Event.RunResumed)
def _measure_run_resumed(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if memory.paused_since is not None:
        memory.pauses.append((memory.paused_since, memory.at))
        memory.paused_since = None
