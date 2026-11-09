"""The kinds `ci_fix` and `address` are Run Stages whose Rounds fix the PR of the Run through the CI fixer.

`ci_fix` works on the red CI of the PR, and `address` on what its inputs read from the Tracker, such as the comments
on the PR.
The fix of a Round lands once the walk leaves the Run Gates after the Stage, as `NodeTools.settle` does.
"""

from factory.core.run_agent import ADDRESSER, CI_FIXER
from factory.pipeline.pipeline import TICKETS, WAIT_CI, RunStage, stage_outcomes
from factory.pipeline.run_kinds import AT_MOST_ONE, NodeRunner, Order, RunNodeKind
from factory.run.node_tools import NodeTools


class FixRunner(NodeRunner):
    """FixRunner works one Round of the Stage through the CI fixer."""

    def __init__(self, stage: RunStage, tools: NodeTools) -> None:
        self.stage = stage
        self.fixer = tools.fixer

    def step(self) -> str:
        return self.fixer.work(self.stage)


ORDER = Order(count=AT_MOST_ONE, after=lambda node: (TICKETS, WAIT_CI), commits=True)

CI_FIX_KIND = RunNodeKind(name="ci_fix", outcomes=stage_outcomes, order=ORDER, agent=CI_FIXER, make=FixRunner)

# A Run in `address` has its PR open, so `factory finish` lands it as one that waited for its CI.
ADDRESS_KIND = RunNodeKind(
    name="address", outcomes=stage_outcomes, order=ORDER, holds_pr=True, agent=ADDRESSER, make=FixRunner
)
