"""The CI Decision is a Decision node of a Run graph, such as `ci_failed`, which puts red CI to the owner and waits for
the answer while the Run beats and takes the owner's Commands.

The registry holds it as the kind `decision`, and the node keeps its `decider`.
"""

from collections.abc import Callable
from pathlib import Path

from factory.core.events import Event
from factory.journal.artifacts import write_once
from factory.journal.commands import RUN_LEVEL
from factory.journal.journal import Journal
from factory.journal.question import render_ci_card
from factory.pipeline.pipeline import WAIT_CI, Decision
from factory.pipeline.run_kinds import DECISION, NodeRunner, Order, RunNodeKind
from factory.ports.run_ports import Ports
from factory.run.inbox import Inbox
from factory.run.node_tools import NodeTools
from factory.run.run_context import TICK_SECONDS, Append, Display, RunContext


class CiDecision:
    """CiDecision asks the owner the Decision of a Run graph on red CI, with a card of the CI and of the CI fixer's
    Rounds."""

    def __init__(
        self,
        context: RunContext,
        journal: Journal,
        append: Append,
        ports: Ports,
        view: Display,
        beat: Callable[[], None],
        inbox: Inbox,
    ) -> None:
        self.journal = journal
        self.append = append
        self.ports = ports
        self.view = view
        self.beat = beat
        self.apply_commands = inbox.apply
        self.tick = TICK_SECONDS
        self.context = context

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


class DecisionRunner(NodeRunner):
    """DecisionRunner asks the owner the Decision through a CiDecision, which only this kind uses."""

    def __init__(self, node: Decision, tools: NodeTools) -> None:
        self.node = node
        run = tools.run_tools
        self.decision = CiDecision(tools.context, run.journal, run.append, run.ports, run.view, run.beat, tools.inbox)

    def step(self) -> str:
        return self.decision.ask(self.node)


# A Decision of a Run graph asks about the CI of the PR, so it comes after a `wait_ci` node.
KIND = RunNodeKind(
    name=DECISION,
    outcomes=lambda node: node.outcomes,
    order=Order(after=lambda node: (WAIT_CI,)),
    make=DecisionRunner,
)
