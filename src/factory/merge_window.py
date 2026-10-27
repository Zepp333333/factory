"""The Merge window gives the Integration branch to one Ticket at a time, in the order the Tickets ask for it."""

from collections.abc import Callable
from typing import Any


class MergeWindow:
    """MergeWindow keeps the Ticket that holds the window and the queue of those waiting, and journals each change.

    A Ticket asks for the window at an Operation and keeps it through Operations and Gates (ADR 0008).
    The Orchestrator releases it when the Ticket enters any other node.
    """

    def __init__(self, append: Callable[[str, dict[str, Any]], None]) -> None:
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
            self._append("MergeWindowQueued", {"ticket": ticket, "node": node})
        if self._holder is not None or self._queue[0] != ticket:
            return False
        self._queue.pop(0)
        self._holder = ticket
        self._append("MergeWindowGranted", {"ticket": ticket, "node": node})
        return True

    def release(self, ticket: str, node: str | None) -> None:
        """Lets the window go when `ticket` holds it, as the Ticket leaves `node`."""
        if self._holder == ticket:
            self._holder = None
            self._append("MergeWindowReleased", {"ticket": ticket, "node": node})
