"""The kind `merge_pr` has the code host merge the Run's PR."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from factory.core import values
from factory.core.events import Event, applies, times
from factory.journal.run_state import RunMemory
from factory.journal.timeline_memory import TimelineMemory
from factory.pipeline.pipeline import OPEN_PR, RETRY, WAIT_CI, RunNode
from factory.pipeline.run_kinds import NodeRunner, Order, Report, RunNodeKind, fixed, parsed, with_block
from factory.ports.tracker import TrackerError
from factory.run.node_tools import NodeTools

MERGE_PR = "merge_pr"

# How a `merge_pr` node has the code host merge the PR.
MERGE_METHODS = ("merge", "squash", "rebase")


@dataclass(frozen=True)
class MergePr:
    """MergePr is how a `merge_pr` node has the code host merge the PR: by `method`, or None for one it rejected."""

    method: str | None


def parse(where: str, body: Mapping[str, Any], report: Report) -> MergePr:
    block = with_block(where, body, report, optional=("method",))
    method = block.fields.get("method", "merge")
    return MergePr(parsed(f"{block.where}.method", method, values.choice(MERGE_METHODS), report))


class MergePrRunner(NodeRunner):
    """MergePrRunner has the code host merge the Run's PR, and holds the node while the Tracker fails, because it may
    hold the merge."""

    def __init__(self, node: RunNode, tools: NodeTools) -> None:
        self.node = node
        self.tools = tools
        self.context = tools.context
        self.append = tools.run_tools.append
        self.project = tools.run_tools.project
        self.pull_requests = tools.run_tools.pull_requests

    def step(self) -> str:
        while (outcome := self._merge(self.node)) is None:
            self.tools.retry()
        if (merge := self.context.memory.merge) != "merged":
            self.tools.run_tools.view.warn(f"{self.node.name}: not merged: {merge}")
        return outcome

    def _merge(self, node: RunNode) -> str | None:
        """Has the code host merge the Run's PR at the commit whose CI went green, and returns the node's outcome.

        It ends `merged` also for a PR that merged before, as one does for a Run killed right after its merge, and
        `refused` for what no retry changes, with the reason in the Journal.
        A Tracker that cannot be read returns None, because it may hold the merge, and the Run tries again.
        `factory check` makes sure the node comes after `opened` of an `open_pr` node, so the Run has its PR.
        """
        pr = self.context.memory.pull_request
        try:
            if self.pull_requests.pushes and self.pull_requests.merged(pr):
                return self._merged(node, pr, self.context.memory.ci_commit)
            why, commit = self._not_mergeable(pr)
            if why:
                return self._refused(node, pr, why)
            why = "the code host took the merge and did not merge the PR"
            try:
                self.pull_requests.merge(pr, commit, node.settings.method)
            except TrackerError as error:
                why = str(error)
            if self.pull_requests.merged(pr):
                return self._merged(node, pr, commit)
            return self._refused(node, pr, why)
        except (TrackerError, OSError) as error:
            self.append(
                Event.TrackerActionFailed,
                {"node": node.name, "target": pr, "action": "merge", "on_fail": RETRY, "reason": str(error)},
            )
            return None

    def _not_mergeable(self, pr: str) -> tuple[str, str]:
        """Returns why the Run's PR may not merge, or nothing and the commit whose CI lets it.

        Only CI that ended `passed` or `absent` lets it, and not the red CI an owner accepted, and the head must still
        be at the commit it ended on, as a Run with no CI at all takes the commit it pushed.
        """
        memory = self.context.memory
        if self.project.landing == "stack":
            return "a stack of PRs merges bottom up, by hand", ""
        if not self.pull_requests.pushes:
            return "the PR is a file, which no code host merges", ""
        if memory.ci is not None or memory.ci_ended not in ("", "passed", "absent"):
            return f"the CI of the PR is not green: {memory.ci.outcome if memory.ci else memory.ci_ended}", ""
        commit = memory.ci_commit or memory.pushed
        head = self.pull_requests.head(pr)
        if head != commit:
            return f"the head of the PR moved from {commit[:7]} to {head[:7]} after its CI", ""
        return "", commit

    def _merged(self, node: RunNode, pr: str, commit: str) -> str:
        self.append(
            Event.PullRequestMerged, {"node": node.name, "url": pr, "commit": commit, "method": node.settings.method}
        )
        return "merged"

    def _refused(self, node: RunNode, pr: str, reason: str) -> str:
        self.append(Event.PullRequestMergeRefused, {"node": node.name, "url": pr, "reason": reason})
        return "refused"


KIND = RunNodeKind(
    name=MERGE_PR,
    outcomes=fixed("merged", "refused"),
    parse=parse,
    order=Order(after=lambda node: (OPEN_PR, WAIT_CI)),
    holds_pr=True,
    make=MergePrRunner,
)


# What the events of the merge tell the readers of the Journal.


@applies(Event.PullRequestMerged)
def _apply_pull_request_merged(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.merge = "merged"


@applies(Event.PullRequestMergeRefused)
def _apply_pull_request_merge_refused(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.merge = payload["reason"]


@times(Event.PullRequestMerged)
def _time_pull_request_merged(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    memory.line("pr", "merged", payload["url"])


@times(Event.PullRequestMergeRefused)
def _time_pull_request_merge_refused(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    memory.line("pr", "merge refused", payload["reason"])
