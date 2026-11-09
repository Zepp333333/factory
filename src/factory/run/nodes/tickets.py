"""The kind `tickets` runs the Ticket Pipeline over the Tickets of the Run."""

from collections.abc import Mapping
from typing import Any

from factory.pipeline.pipeline import TICKETS
from factory.pipeline.run_kinds import ONE, TICKETS_SHAPE, Order, Report, RunNodeKind, fixed, with_block


def parse(where: str, body: Mapping[str, Any], report: Report) -> None:
    """Reads the `with` of a `tickets` node, whose `pipeline` the project file reads before the Run graph loads."""
    with_block(where, body, report, required=("pipeline",))


KIND = RunNodeKind(
    name=TICKETS,
    outcomes=fixed("all_merged", "some_merged", "none_merged"),
    parse=parse,
    order=Order(count=ONE),
    shape=TICKETS_SHAPE,
)
