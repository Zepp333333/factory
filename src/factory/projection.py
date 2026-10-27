"""The Projection hands the Tracker the events a human cares about, through a cursor over the Run's journal.

A Decision that waits for the owner becomes a comment with its question card and the label `factory:needs-human`,
and the owner's answer takes the label off and publishes the outcome.
Delivery is at least once: the cursor moves only after the Tracker took an event, so a failed or interrupted
delivery repeats, and `resume` continues from the cursor.
A Tracker that cannot be reached fails the delivery and never the Run, and nothing the Tracker holds flows back.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from factory.commands import RUN_LEVEL
from factory.journal import EventRow, Journal
from factory.tracker import Tracker, TrackerError

NEEDS_HUMAN = "factory:needs-human"


class Projection:
    def __init__(
        self, journal: Journal, run_id: str, tracker: Tracker, record: Callable[[str, dict[str, Any]], None]
    ) -> None:
        self.journal = journal
        self.run_id = run_id
        self.tracker = tracker
        self.record = record

    def deliver(self) -> str | None:
        """Delivers the events after the cursor that matter to a human, in order.

        Returns why it stopped early, or None when it caught up.
        """
        for seq, event in self.journal.events_after(self.run_id, self.journal.tracker_cursor(self.run_id)):
            try:
                if not self._send(seq, event):
                    continue
            except (TrackerError, OSError) as error:
                return f"event {seq} ({event.type}): {error}"
            self.record("TrackerDelivered", {"seq": seq, "type": event.type})
        return None

    def _send(self, seq: int, event: EventRow) -> bool:
        """Sends what the event means to the Tracker and returns whether it meant anything to a human."""
        payload = event.payload
        ticket = payload.get("ticket")
        if ticket is None or ticket == RUN_LEVEL:
            return False
        if event.type == "DecisionWaiting":
            self.tracker.comment(ticket, seq, _read(Path(payload["card"]), f"Decision {payload['node']} waits."))
            self.tracker.add_label(ticket, NEEDS_HUMAN)
        elif event.type in ("DecisionDecided", "DecisionOverridden") and payload.get("decider") == "human":
            self.tracker.remove_label(ticket, NEEDS_HUMAN)
            self.tracker.comment(ticket, seq, _outcome(payload))
        else:
            return False
        return True


def _outcome(payload: dict[str, Any]) -> str:
    text = f"Decision {payload['node']} of Ticket {payload['ticket']} was decided: {payload['outcome']}."
    reasons = payload.get("reasons") or []
    return text + "".join(f"\n- {reason}" for reason in reasons)


def _read(path: Path, fallback: str) -> str:
    try:
        return path.read_text()
    except OSError:
        return fallback
