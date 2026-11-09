"""The kind `describe` is the Run Stage of the Describer, which writes the text of the PRs the Landing plans."""

from factory.core.run_agent import DESCRIBER
from factory.pipeline.pipeline import OPEN_PR, TICKETS, RunStage, stage_outcomes
from factory.pipeline.run_kinds import AT_MOST_ONE, NodeRunner, Order, RunNodeKind
from factory.run.node_tools import NodeTools


class DescribeRunner(NodeRunner):
    """DescribeRunner has the Describer write the PR text through the Lander, also for a Run the owner finishes."""

    def __init__(self, stage: RunStage, tools: NodeTools) -> None:
        self.stage = stage
        self.lander = tools.lander

    def step(self) -> str:
        return self.lander.describe(self.stage)

    def finish(self) -> None:
        self.lander.describe(self.stage)


KIND = RunNodeKind(
    name="describe",
    outcomes=stage_outcomes,
    order=Order(count=AT_MOST_ONE, after=lambda node: (TICKETS,), before=(OPEN_PR,)),
    agent=DESCRIBER,
    make=DescribeRunner,
)
