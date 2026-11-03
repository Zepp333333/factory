"""The Merge window gives the Integration branch to one Ticket at a time, in the order the Tickets ask for it."""

from collections.abc import Callable
from typing import Any

from factory.core.events import Event, applies, measures
from factory.journal.run_state import RunMemory
from factory.views.metrics import MetricsMemory


class MergeWindow:
    """MergeWindow keeps the Ticket that holds the window and the queue of those waiting, and journals each change.

    A Ticket asks for the window at an Operation and keeps it through Operations and Gates (ADR 0008).
    The Orchestrator releases it when the Ticket enters any other node.
    """

    def __init__(self, append: Callable[[Event, dict[str, Any]], None]) -> None:
        self._append = append
        self._holder: str | None = None
        self._queue: list[str] = []

    def restore(self, holder: str | None, queue: list[str]) -> None:
        """Puts back the holder and queue a resumed Run had, without journaling them again."""
        self._holder, self._queue = holder, list(queue)

    def hold(self, ticket: str, node: str) -> bool:
        """Returns whether `ticket` holds the window, queueing it on its first ask and granting the window in turn."""
        if self._holder == ticket:
            return True
        if ticket not in self._queue:
            self._queue.append(ticket)
            self._append(Event.MergeWindowQueued, {"ticket": ticket, "node": node})
        if self._holder is not None or self._queue[0] != ticket:
            return False
        self._queue.pop(0)
        self._holder = ticket
        self._append(Event.MergeWindowGranted, {"ticket": ticket, "node": node})
        return True

    def release(self, ticket: str, node: str | None) -> None:
        """Lets the window go when `ticket` holds it, as the Ticket leaves `node`."""
        if self._holder == ticket:
            self._holder = None
            self._append(Event.MergeWindowReleased, {"ticket": ticket, "node": node})

    def forget(self, ticket: str) -> None:
        """Takes `ticket` out of the queue when it stops asking, as a cancelled or overridden Ticket does."""
        if ticket in self._queue:
            self._queue.remove(ticket)
            self._append(Event.MergeWindowLeft, {"ticket": ticket})


# What the events of the Merge window tell the readers of the Journal.


@applies(Event.MergeWindowQueued)
def _apply_merge_window_queued(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.queue.append(payload["ticket"])


@applies(Event.MergeWindowLeft)
def _apply_merge_window_left(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.queue.remove(payload["ticket"])


@applies(Event.MergeWindowGranted)
def _apply_merge_window_granted(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.queue.remove(payload["ticket"])
    memory.holder = payload["ticket"]


@applies(Event.MergeWindowReleased)
def _apply_merge_window_released(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.holder = None


@measures(Event.MergeWindowQueued)
def _measure_merge_window_queued(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is not None:
        memory.queued[ticket.ticket] = memory.at


@measures(Event.MergeWindowGranted)
def _measure_merge_window_granted(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is None:
        return
    since = memory.queued.pop(ticket.ticket, None)
    if since is not None:
        ticket.wait_merge_window_s += (memory.at - since).total_seconds()
