"""The Run custody says which process owns a Run: it is taken, beaten, released and checked in the Journal.

The Orchestrator of a Run beats a heartbeat into the Journal, and a Run that has not beaten for the stale time is
orphaned by any other process, which frees its Slots.
Only `resume` and `finish` take an orphaned Run again, and exactly one of them does.
"""

from datetime import datetime
from typing import Any

from factory.clock import Clock
from factory.events import Event
from factory.journal import Journal
from factory.states import RunState

# The Run beats this often, in seconds, and another process orphans a Run that has not beaten for the stale time.
HEARTBEAT_SECONDS = 10
HEARTBEAT_STALE_SECONDS = 60


class RunTakenOver(Exception):
    """A RunTakenOver means another process orphaned or took this Run, so this process must stop writing to it."""


class RunCustody:
    """A RunCustody is one process's custody of the Runs of the Journal."""

    def __init__(self, journal: Journal, clock: Clock) -> None:
        self._journal = journal
        self._clock = clock
        self._beat: datetime | None = None

    def begin(self) -> None:
        """Counts the next beat from now, for a Run whose `RunStarted` was just journaled."""
        self._beat = self._clock.now()

    @property
    def began(self) -> bool:
        """Tells whether this process began a Run or reclaimed one."""
        return self._beat is not None

    def reclaim(self, run_id: str, payload: dict[str, Any]) -> None:
        """Takes an orphaned Run, journals `RunReclaimed` with `payload` and starts beating for it.

        The state check and the event are one transaction, so of two processes that reclaim one Run only one wins
        and the other gets a RunTakenOver.
        """
        with self._journal.transaction():
            run = self._journal.run(run_id)
            if run is None or run.state != RunState.ORPHANED:
                raise RunTakenOver(
                    f"Run {run_id} was {run.state if run else 'removed'}, not orphaned, when it was taken"
                )
            self._journal.append(run_id, Event.RunReclaimed, payload)
        self._beat = self._clock.now()

    def beat(self, run_id: str) -> None:
        """Journals a heartbeat once the interval has passed, and orphans the other Runs whose beat went stale.

        A Run that finds itself orphaned was too slow to beat, and another process may be resuming it,
        so it raises a RunTakenOver.
        """
        now = self._clock.now()
        if self._beat is not None and (now - self._beat).total_seconds() < HEARTBEAT_SECONDS:
            return
        with self._journal.transaction():
            if not self.holds(run_id):
                run = self._journal.run(run_id)
                raise RunTakenOver(f"Run {run_id} was {run.state if run else 'removed'} by another process")
            self._journal.append(run_id, Event.Heartbeat, {})
        self._beat = now
        self._journal.orphan_stale(HEARTBEAT_STALE_SECONDS, keep=run_id)

    def release(self, run_id: str, reason: str) -> bool:
        """Leaves the Run orphaned at once and returns whether this process still held it."""
        with self._journal.transaction():
            held = self.holds(run_id)
            if held:
                self._journal.append(run_id, Event.RunOrphaned, {"silent_seconds": 0, "reason": reason})
        return held

    def holds(self, run_id: str) -> bool:
        """Tells whether the Run is still `running`, which is to say no other process took it from this one."""
        run = self._journal.run(run_id)
        return run is not None and run.state == RunState.RUNNING

    def orphan_stale(self) -> list[str]:
        """Orphans every Run whose heartbeat went stale and returns their ids."""
        return self._journal.orphan_stale(HEARTBEAT_STALE_SECONDS)

    def is_live(self, run_id: str) -> bool:
        """Tells whether a process owns the Run, after orphaning every Run whose heartbeat went stale."""
        self.orphan_stale()
        return self.holds(run_id)
