"""The kind `open_pr` pushes the Integration branch and opens the Run's PR."""

from factory.pipeline.pipeline import OPEN_PR, TICKETS, RunNode
from factory.pipeline.run_kinds import NodeRunner, Order, RunNodeKind, fixed
from factory.run.node_tools import NodeTools


class OpenPrRunner(NodeRunner):
    """OpenPrRunner opens the PRs the Landing plans through the Lander."""

    def __init__(self, node: RunNode, tools: NodeTools) -> None:
        self.lander = tools.lander
        self.graph = tools.run_tools.project.run_graph

    def step(self) -> str:
        self.lander.open_pr()
        return self.lander.outcome

    def finish(self) -> None:
        """Lands what a Run that the owner finishes merged, after the Describer of the graph, if any, wrote the text.

        The Describer is a Run Stage, which has no runner of its own until Run Stages name their kind, so this node has
        it write the text first.
        """
        assert self.graph is not None
        if (describer := self.graph.describer_stage) is not None:
            self.lander.describe(describer)
        self.lander.open_pr()


KIND = RunNodeKind(
    name=OPEN_PR, outcomes=fixed("opened", "failed"), order=Order(after=lambda node: (TICKETS,)), make=OpenPrRunner
)
