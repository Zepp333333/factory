"""The Orchestrator walks the Run graph of one Run, through the runner that the kind of each node makes."""

from typing import Any

from factory.core.events import Event, applies, measures, nothing, times
from factory.core.states import RUN_TERMINAL_NODES, RunState, TicketState
from factory.journal.metrics_memory import MetricsMemory
from factory.journal.run_state import RunMemory
from factory.journal.timeline_memory import TimelineMemory, duration
from factory.pipeline.pipeline import Decision, Gate, RunNode, RunStage
from factory.run.ci_decision import CiDecision
from factory.run.node_tools import NodeTools
from factory.run.nodes.plan import PlanRunner
from factory.run.operations import RUN_NODE_ACTS
from factory.run.run_context import RunContext
from factory.run.run_tools import RunTools

# The reason a Run ends with when the owner answers its Decision with `abort`.
OWNER_ABORTED = "the owner aborted the Run after Attempts failed in a row"

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


class Orchestrator:
    """The Orchestrator walks the Run graph of its context with the tools of the Run.

    It enters a node, has the node's kind make its runner, steps it, journals how it ended and routes the outcome.
    Only `append` of the tools changes the Run's state, and the runners read the same context.
    The Orchestrator is the only phase that takes the tools whole: it builds the NodeTools from them once.
    """

    def __init__(self, context: RunContext, tools: RunTools) -> None:
        self.context = context
        self.tools = tools
        self.journal = tools.journal
        self.project = tools.project
        self.pipeline = tools.pipeline
        self.view = tools.view
        self.custody = tools.custody
        self.attempts = tools.attempts
        self.nodes = NodeTools(context, tools)
        self.run_start = self.nodes.run_start
        self.ci_decision = CiDecision(
            context, tools.journal, tools.append, tools.ports, tools.view, tools.beat, self.nodes.inbox
        )

    @property
    def run_id(self) -> str:
        return self.context.run_id

    @property
    def memory(self) -> RunMemory:
        return self.context.memory

    @property
    def started(self) -> bool:
        """Tells whether this process began the Run or took it up, so that it has a Run to end."""
        return self.custody.began

    @property
    def pull_request(self) -> str:
        return self.memory.pull_request

    @property
    def by_hand(self) -> list[list[str]]:
        """Returns the commands that finish a landing that failed by hand."""
        return self.nodes.lander.by_hand

    @property
    def ci_note(self) -> str:
        """Returns how the CI of the Run ended as the Run's report tells it, also for red CI the Run graph left red."""
        wait = self.memory.ci
        return UNACCEPTED_NOTES.get(wait.outcome, "") if wait else CI_NOTES.get(self.memory.ci_ended, "")

    @property
    def merge_note(self) -> str:
        """Returns what the Run's report says of the merge of its PR, or nothing for a Run that tried none."""
        merge = self.memory.merge
        return merge and ("merged" if merge == "merged" else f"not merged: {merge}")

    def start(self) -> None:
        """Starts the Run on the snapshot the Tracker gave, and lands it through the PullRequests the Run was given."""
        self.run_start.begin()

    def resume(self) -> None:
        """Takes up an orphaned Run from its events and puts its Tickets back where the journal says they were."""
        self.run_start.resume()

    def finish(self) -> RunState:
        """Ends an orphaned Run as `partial` with what it merged, and lands that.

        Each Run node does what `factory finish` asks of its kind, so `open_pr` lands what merged, and `wait_ci` ends
        the wait of a Run orphaned in phase `ci`, which has landed already, as unchecked CI.
        A Run that has landed ends in the state of any Run whose Tickets all merged, and so does a Run orphaned while it
        waits for the Tracker, addresses the comments on its PR or merges it, which ends without the event, the Round
        or the merge.
        No session of an unfinished Ticket comes back: each Ticket that did not merge is abandoned,
        with its unsaved work kept under its ref, and the Run lands like any Run that ends `partial`.
        """
        state = self.run_start.reclaim()
        for ticket in self.context.tickets:
            if ticket.state in (TicketState.QUEUED, TicketState.ACTIVE):
                self.run_start.give_up(ticket, state.in_flight.get(ticket.id), ticket.id in state.probing)
        self.nodes.deliver(last=True)
        graph = self.project.run_graph
        assert graph is not None
        landed = self.memory.ci is not None or self.memory.run_node in graph.after_the_pr
        for node in graph.nodes.values():
            if isinstance(node, RunNode):
                node.spec.make(node, self.nodes).finish()
        if landed:
            self._finish(self._final_state())
            return self._final_state()
        self._finish(RunState.PARTIAL)
        return RunState.PARTIAL

    def run(self) -> RunState:
        """Walks the Run graph from the node the Journal ends in, or its start, and returns the Run's final state.

        A Run that resumes goes on in the node it was in, and a Journal without Run node events starts at the start of
        the default Run graph, whose nodes do again only what the Journal does not hold yet.
        A fix of the CI fixer lands as the walk leaves its Run Gates, or its Stage when it has none.
        A node that ends in no outcome is a Run the owner aborted.
        """
        graph = self.project.run_graph
        assert graph is not None
        self.view.show(self.journal, self.run_id)
        if self.memory.run_node or self.memory.run_next:
            # A later node does not tick, so the Tracker gets here what a delivery before the kill failed to hand.
            self.nodes.deliver(last=True)
        while True:
            name = self.memory.run_node or self.memory.run_next or graph.start
            if self.memory.run_node is None and not isinstance(graph.nodes.get(name), Gate):
                self.nodes.fixer.land()
            if name in RUN_TERMINAL_NODES:
                break
            node = graph.nodes[name]
            assert isinstance(node, RunNode | RunStage | Gate | Decision), "Config proves what a Run graph holds"
            if self.memory.run_node is None:
                kind = node.KIND if isinstance(node, Decision) else node.kind
                self.tools.append(Event.RunNodeEntered, {"node": name, "kind": kind})
                self.view.show(self.journal, self.run_id)
            outcome = self._step_run_node(node)
            if outcome is None:
                self.abort(OWNER_ABORTED)
                return RunState.ABORTED
            self.tools.append(
                Event.RunNodeEnded, {"node": name, "outcome": outcome, "next": graph.route(node, outcome)}
            )
        state = self._final_state()
        self._finish(state)
        return state

    def _step_run_node(self, node: RunNode | RunStage | Gate | Decision) -> str | None:
        """Carries out a node of the Run graph and returns its outcome, or None for a Run the owner aborted.

        A Run node runs through the runner its kind makes, and a Run Stage, a Run Gate and a Run Decision through the
        phase that does their work, until they name a kind of their own.
        """
        match node:
            case RunNode():
                return node.spec.make(node, self.nodes).step()
            case RunStage(selects=True):
                return PlanRunner(node, self.nodes).step()
            case RunStage(commits=True):
                return self.nodes.fixer.work(node)
            case RunStage():
                return self.nodes.lander.describe(node)
            case Gate():
                return self.nodes.fixer.run_gate(node)
            case Decision():
                return self.ci_decision.ask(node)

    def _final_state(self) -> RunState:
        return RunState.DONE if self.memory.finished else RunState.PARTIAL

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
        for ticket in self.context.tickets:
            if ticket.running is not None:
                self.attempts.kill(ticket.running)
            if ticket.probe is not None:
                ticket.probe.process.kill()
        return not taken_over

    def _finish(self, state: RunState, reason: str | None = None) -> None:
        self.tools.append(Event.RunFinished, {"state": state, **({"reason": reason} if reason else {})})
        self.view.show(self.journal, self.run_id)


# What the events the Orchestrator writes tell the readers of the Journal.

applies(Event.RunFinished)(nothing)


@measures(Event.RunNodeEntered)
def _measure_run_node_entered(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    memory.enter_run_node(payload["node"])


@measures(Event.RunNodeEnded)
def _measure_run_node_ended(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    memory.leave_run_node(memory.at, payload["outcome"])


@applies(Event.RunNodeEntered)
def _apply_run_node_entered(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.run_node, memory.run_next, memory.run_decided = payload["node"], None, None
    memory.tracker_wait, memory.merge = None, ""
    memory.acted.pop(RUN_NODE_ACTS, None)


@applies(Event.RunNodeEnded)
def _apply_run_node_ended(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.run_node, memory.run_next = None, payload["next"]


@times(Event.RunFinished)
def _time_run_finished(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    memory.close_all()
    took = duration((memory.at - memory.started).total_seconds()) if memory.started else "-"
    memory.line("run", payload["state"], f"{took}  ${memory.total_cost:.2f}")


@measures(Event.RunFinished)
def _measure_run_finished(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    memory.ended, memory.state = memory.at, str(payload.get("state"))
