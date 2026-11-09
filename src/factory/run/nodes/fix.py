"""The kinds `ci_fix` and `address` are Run Stages whose Rounds fix the PR of the Run through the CiFixer.

`ci_fix` works on the red CI of the PR, and `address` on what its inputs read from the Tracker, such as the comments
on the PR.
The fix of a Round lands once the walk leaves the Run Gates after the Stage, as `NodeTools.settle` does.
"""

from factory.pipeline.pipeline import (
    PR_COMMENTS,
    SPEC_TARGET,
    TICKETS,
    TRACKER_INPUTS,
    WAIT_CI,
    RunStage,
    stage_outcomes,
)
from factory.pipeline.run_kinds import AT_MOST_ONE, NodeRunner, Order, RunNodeKind
from factory.run.ci_fix import ADDRESSER, CI_FIXER
from factory.run.node_tools import NodeTools


class FixRunner(NodeRunner):
    """FixRunner works one Round of the Stage through the CiFixer, as the agent of the Stage's kind."""

    def __init__(self, stage: RunStage, tools: NodeTools) -> None:
        self.stage = stage
        self.fixer = tools.fixer

    def step(self) -> str:
        return self.fixer.work(self.stage)


ORDER = Order(count=AT_MOST_ONE, after=lambda node: (TICKETS, WAIT_CI), commits=True)

# A Round of `ci_fix` works on the red CI, and may read the Spec too.
CI_FIX_KIND = RunNodeKind(
    name="ci_fix", outcomes=stage_outcomes, order=ORDER, agent=CI_FIXER, takes=(SPEC_TARGET,), make=FixRunner
)

# A Round of `address` works on the comments on the PR, so the Stage reads them, and a Run in it has its PR open,
# which `factory finish` lands as one that waited for its CI.
ADDRESS_KIND = RunNodeKind(
    name="address",
    outcomes=stage_outcomes,
    order=ORDER,
    holds_pr=True,
    agent=ADDRESSER,
    takes=(SPEC_TARGET, *TRACKER_INPUTS),
    takes_one_of=(PR_COMMENTS,),
    make=FixRunner,
)
