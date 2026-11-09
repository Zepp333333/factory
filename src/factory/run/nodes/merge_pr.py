"""The kind `merge_pr` has the code host merge the Run's PR."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from factory.core import values
from factory.pipeline.pipeline import MERGE_METHODS, MERGE_PR, OPEN_PR, WAIT_CI
from factory.pipeline.run_kinds import Order, Report, RunNodeKind, fixed, parsed, with_block


@dataclass(frozen=True)
class MergePr:
    """MergePr is how a `merge_pr` node has the code host merge the PR: by `method`, or None for one it rejected."""

    method: str | None


def parse(where: str, body: Mapping[str, Any], report: Report) -> MergePr:
    block = with_block(where, body, report, optional=("method",))
    method = block.fields.get("method", "merge")
    return MergePr(parsed(f"{block.where}.method", method, values.choice(MERGE_METHODS), report))


KIND = RunNodeKind(
    name=MERGE_PR,
    outcomes=fixed("merged", "refused"),
    parse=parse,
    order=Order(after=lambda node: (OPEN_PR, WAIT_CI)),
    holds_pr=True,
)
