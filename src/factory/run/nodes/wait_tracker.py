"""The kind `wait_tracker` waits for an event on the PR or the Spec, and ends in that event or in `timeout`."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from factory.core import values
from factory.pipeline.pipeline import (
    OPEN_PR,
    PR_TARGET,
    REVIEW_EVENTS,
    SPEC_TARGET,
    TRACKER_EVENTS,
    WAIT_TRACKER,
    WAIT_TRACKER_TARGETS,
)
from factory.pipeline.run_kinds import WAIT_SHAPE, Order, Report, RunNodeKind, names, parsed, with_block

TIMED_OUT = "timeout"


@dataclass(frozen=True)
class WaitTracker:
    """WaitTracker is what a `wait_tracker` node waits for: the events `on` its target, until its `timeout`."""

    on: tuple[str, ...]
    timeout: float | None
    target: str | None


def parse(where: str, body: Mapping[str, Any], report: Report) -> WaitTracker:
    """Reads the `with` of a `wait_tracker`: the events `on` its target, by default the PR, and its `timeout`."""
    block = with_block(where, body, report, required=("on", "timeout"), optional=("target",))
    at, fields = block.where, block.fields
    target = parsed(f"{at}.target", fields.get("target", PR_TARGET), values.choice(WAIT_TRACKER_TARGETS), report)
    on = names(at, fields, "on", report)
    if "on" in fields and not on:
        report(f"{at}.on", "names no event to wait for")
    for event in on:
        if parsed(f"{at}.on", event, values.choice(TRACKER_EVENTS), report) in REVIEW_EVENTS and target == SPEC_TARGET:
            report(f"{at}.on", f"has {event}, which only a PR gets")
    timeout = parsed(f"{at}.timeout", fields["timeout"], values.duration, report) if "timeout" in fields else None
    return WaitTracker(on, timeout, target)


def after(node: Any) -> tuple[str, ...]:
    """Returns the kinds that have run on every path to the node, where a wait for an event on the PR needs the PR."""
    return (OPEN_PR,) if node.settings.target == PR_TARGET else ()


KIND = RunNodeKind(
    name=WAIT_TRACKER,
    outcomes=lambda node: (*node.settings.on, TIMED_OUT),
    parse=parse,
    order=Order(after=after),
    holds_pr=True,
    shape=WAIT_SHAPE,
)
