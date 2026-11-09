"""The kind `describe` is the Run Stage of the Describer, which writes the text of the PRs the Landing plans."""

from factory.core.run_agent import DESCRIBER
from factory.pipeline.pipeline import OPEN_PR, TICKETS, TRACKER_INPUTS, RunStage, stage_outcomes
from factory.pipeline.run_kinds import AT_MOST_ONE, NodeRunner, Order, RunNodeKind
from factory.run.node_tools import NodeTools


class DescribeRunner(NodeRunner):
    """DescribeRunner has the Describer write the PR text through the Lander, also for a Run the owner finishes."""

    def __init__(self, stage: RunStage, tools: NodeTools) -> None:
        self.stage = stage
        self.lander = tools.lander
        self.graph = tools.run_tools.project.run_graph

    def step(self) -> str:
        return self.lander.describe(self.stage)

    def finish(self) -> None:
        """Writes the PR text of a Run the owner finishes, when the graph has an `open_pr` node that takes it."""
        assert self.graph is not None
        if self.graph.run_nodes_of(OPEN_PR):
            self.lander.describe(self.stage)


KIND = RunNodeKind(
    name="describe",
    outcomes=stage_outcomes,
    order=Order(count=AT_MOST_ONE, after=lambda node: (TICKETS,), before=(OPEN_PR,)),
    agent=DESCRIBER,
    takes=TRACKER_INPUTS,
    make=DescribeRunner,
)
