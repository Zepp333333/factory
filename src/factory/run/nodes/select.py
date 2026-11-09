"""The kinds `all` and `filter` select the Tickets of the Run from those the Tracker read."""

from collections.abc import Mapping
from typing import Any

from factory.pipeline.pipeline import ALL, EMPTY, FILTER, SELECT, SELECTED, TICKETS
from factory.pipeline.run_kinds import AT_MOST_ONE, Order, Report, RunNodeKind, fixed, names, with_block

ORDER = Order(count=AT_MOST_ONE, group=SELECT, before=(TICKETS,))


def parse_all(where: str, body: Mapping[str, Any], report: Report) -> dict[str, Any]:
    with_block(where, body, report)
    return {}


def parse_filter(where: str, body: Mapping[str, Any], report: Report) -> dict[str, Any]:
    """Reads the `with` of a `filter`, whose `label` and `without` each name one label or a list of them."""
    block = with_block(where, body, report, optional=("label", "without"))
    lists = {key: [value] if isinstance(value, str) else value for key, value in block.fields.items()}
    found = {key: list(names(block.where, lists, key, report)) for key in ("label", "without")}
    if not found["label"] and not found["without"]:
        report(block.where, "names no label to take or leave out")
    return found


def filter_labels(node: Any) -> tuple[str, ...]:
    return (*node.settings["label"], *node.settings["without"])


ALL_KIND = RunNodeKind(name=ALL, outcomes=fixed(SELECTED, EMPTY), parse=parse_all, order=ORDER)
FILTER_KIND = RunNodeKind(
    name=FILTER, outcomes=fixed(SELECTED, EMPTY), parse=parse_filter, order=ORDER, labels=filter_labels
)
