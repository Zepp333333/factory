"""The kinds `assign`, `label` and `comment` act on the Tracker, on each Ticket of the Run or on the Spec."""

from collections.abc import Mapping
from typing import Any

from factory.pipeline import tracker_action
from factory.pipeline.pipeline import TRACKER_OPERATIONS, TrackerAction
from factory.pipeline.run_kinds import Report, RunNodeKind


def _kind(name: str) -> RunNodeKind:
    def parse(where: str, body: Mapping[str, Any], report: Report) -> TrackerAction:
        return tracker_action.parse(where, body, name, True, report)

    return RunNodeKind(
        name=name,
        outcomes=lambda node: node.settings.outcomes,
        parse=parse,
        labels=lambda node: (*node.settings.add, *node.settings.remove),
    )


KINDS = tuple(_kind(name) for name in TRACKER_OPERATIONS)
