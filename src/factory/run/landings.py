"""A Landing is how a Run lands, one implementation per `landing:` of the project file (ADR 0016).

It plans the PRs the Run opens, names the branch of the PR the Run waits on, moves a fix to that PR's head and says
whether the Run may merge.
Every module that depends on the mode asks the Landing, and none of them names a mode.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from factory.core.git import GitError, HostGit
from factory.core.names import layer_branch
from factory.core.states import TicketState
from factory.ports.tracker import PullRequests
from factory.run.live import LiveTicket
from factory.run.run_context import RunContext


def landing_remote(git: HostGit, pull_requests: PullRequests) -> str:
    """Returns the first git remote, by name, that points at the repository the PR opens in."""
    for name, url in sorted(git.remotes().items()):
        if pull_requests.accepts(url):
            return name
    raise GitError(f"no git remote of {git.repo} points at {pull_requests.url()}")


@dataclass(frozen=True)
class PlannedPr:
    """PlannedPr is one PR a Run opens.

    `number` is 0 for the one PR of a Run that opens one, and the layer's number, from 1 at the bottom, in a stack.
    The PR opens from `head` at `commit` into `base` at `base_commit`, which the Describer diffs against.
    `tickets` are the Tickets its footer names, and `title` is what the list of a stack calls it.
    `top` tells that this is the PR the Run waits on, whose URL is the Run's PR.
    """

    number: int
    title: str
    head: str
    commit: str
    base: str
    base_commit: str
    tickets: tuple[LiveTicket, ...]
    top: bool


class Landing(ABC):
    """Landing answers what depends on how a Run lands, for the Run of `context`, and `NAME` is its `landing:`."""

    NAME: ClassVar[str]

    def __init__(self, context: RunContext, git: HostGit, pull_requests: PullRequests, base: str) -> None:
        self.context = context
        self.git = git
        self.pull_requests = pull_requests
        self.base = base

    @abstractmethod
    def plan(self) -> list[PlannedPr]:
        """Returns the PRs the Run opens for the Tickets it merged, from the bottom up, the top one last."""

    @abstractmethod
    def watched(self) -> str:
        """Returns the branch at the head of the PR the Run waits on."""

    @abstractmethod
    def land_fix(self, fixed: str, tip: str) -> tuple[str, str]:
        """Moves the branches the fix lands on from the Integration branch's `tip` to `fixed`, pushes `watched()`, and
        returns the remote and the branch it pushed.

        A Run killed between two moves moves only what is not at `fixed` yet.
        It raises GitError when a move or the push fails.
        """

    def merge_refusal(self) -> str:
        """Returns why the Run may not merge its PR, or nothing when the node `merge_pr` holds it to its own rules."""
        return ""

    def notes(self, pr: PlannedPr, plan: list[PlannedPr]) -> str:
        """Returns the text the engine appends to the body of `pr` after its footer, or nothing."""
        return ""

    def _merged(self) -> list[LiveTicket]:
        """Returns the merged Tickets in the order the Integration branch merged them."""
        merged = {t.id: t for t in self.context.tickets if t.state == TicketState.MERGED}
        return [merged[landing.ticket] for landing in self.context.memory.landings if landing.ticket in merged]

    def _move(self, branch: str, fixed: str, tip: str) -> None:
        if self.git.resolve(branch) != fixed:
            self.git.fast_forward(branch, fixed, tip)

    def _push(self, branch: str) -> tuple[str, str]:
        remote = landing_remote(self.git, self.pull_requests)
        self.git.push(remote, branch)
        return remote, branch


class SingleLanding(Landing):
    """SingleLanding opens one PR from the Integration branch into the base, with every Ticket of the Run.

    The footer of that PR closes the merged Tickets and names the others with why they did not land.
    """

    NAME = "single"

    def plan(self) -> list[PlannedPr]:
        context = self.context
        commit = self.git.resolve(context.integration)
        title = context.snapshot.title
        tickets = tuple(context.tickets)
        return [PlannedPr(0, title, context.integration, commit, self.base, context.base_commit, tickets, True)]

    def watched(self) -> str:
        return self.context.integration

    def land_fix(self, fixed: str, tip: str) -> tuple[str, str]:
        self._move(self.context.integration, fixed, tip)
        return self._push(self.context.integration)


class StackLanding(Landing):
    """StackLanding opens one PR per merged Ticket, each into the branch of the layer below.

    Each layer's branch is at the commit where its Ticket merged, and the top one at the Integration branch's tip.
    A fix moves the Integration branch and the top layer's branch together, so they stay on one commit and a plan made
    again after `resume` or a fix finds the top layer where the PRs opened it.
    Only the top PR's CI is the Run's CI, and a stack merges through its top PR, by hand.
    """

    NAME = "stack"

    def plan(self) -> list[PlannedPr]:
        context = self.context
        merged = self._merged()
        after = {landing.ticket: landing.after for landing in context.memory.landings}
        titles = {t.id: t.title for t in context.snapshot.tickets}
        planned: list[PlannedPr] = []
        below, below_commit = self.base, context.base_commit
        for number, ticket in enumerate(merged, 1):
            top = number == len(merged)
            commit = self.git.resolve(context.integration) if top else after[ticket.id]
            title = titles[ticket.id]
            head = layer_branch(context.run_id, number, title)
            planned.append(PlannedPr(number, title, head, commit, below, below_commit, (ticket,), top))
            below, below_commit = head, commit
        return planned

    def watched(self) -> str:
        merged = self._merged()
        titles = {t.id: t.title for t in self.context.snapshot.tickets}
        return layer_branch(self.context.run_id, len(merged), titles[merged[-1].id])

    def land_fix(self, fixed: str, tip: str) -> tuple[str, str]:
        top = self.watched()
        self._move(self.context.integration, fixed, tip)
        self._move(top, fixed, tip)
        return self._push(top)

    def merge_refusal(self) -> str:
        return "a stack of PRs merges bottom up, by hand"

    def notes(self, pr: PlannedPr, plan: list[PlannedPr]) -> str:
        """Lists the layers of the stack, says that the top PR carries the fixes of the layers below it, and asks for a
        merge bottom up."""
        stack = "\n".join(
            f"{other.number}. {other.title} (`{other.head}`)" + (" **<- this PR**" if other is pr else "")
            for other in plan
        )
        return (
            f"Stack of {len(plan)} PRs, from the bottom up:\n\n{stack}\n\n"
            "The top PR carries every fix of the CI the Run made, so its CI is the verdict of the stack, "
            "and a PR below it may stay red for a fault a layer above fixed.\n\n"
            "Merge the stack bottom up, with a merge commit only (no squash, no rebase), "
            f"and delete the branch after each merge, so the code host retargets the next PR to `{self.base}`."
        )


# The Landings by the name a project file gives `landing:`, which `wiring` hands `config` as the names it accepts.
LANDINGS: dict[str, type[Landing]] = {kind.NAME: kind for kind in (SingleLanding, StackLanding)}
