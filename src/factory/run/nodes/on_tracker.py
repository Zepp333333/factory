"""The kinds `assign`, `label` and `comment` act on the Tracker, on each Ticket of the Run or on the Spec."""

from collections.abc import Mapping
from typing import Any

from factory.pipeline import tracker_action
from factory.pipeline.pipeline import TRACKER_OPERATIONS, RunNode, TrackerAction
from factory.pipeline.run_kinds import NodeRunner, Report, RunNodeKind
from factory.run.node_tools import NodeTools
from factory.run.run_context import TICK_SECONDS


class TrackerRunner(NodeRunner):
    """TrackerRunner acts on the targets of the node, and holds the node while it waits to try the Tracker again."""

    def __init__(self, node: RunNode, tools: NodeTools) -> None:
        self.node = node
        self.tools = tools

    def step(self) -> str:
        while (outcome := self.tools.tracker_operations.act_for_run(self.node)) is None:
            self.tools.hold(TICK_SECONDS)
        return outcome


def _kind(name: str) -> RunNodeKind:
    def parse(where: str, body: Mapping[str, Any], report: Report) -> TrackerAction:
        return tracker_action.parse(where, body, name, True, report)

    return RunNodeKind(
        name=name,
        outcomes=lambda node: node.settings.outcomes,
        parse=parse,
        labels=lambda node: (*node.settings.add, *node.settings.remove),
        make=TrackerRunner,
    )


KINDS = tuple(_kind(name) for name in TRACKER_OPERATIONS)
