"""Recovery rebuilds what the Orchestrator keeps in memory about a Run from the Run's events.

`replay` folds the events of an orphaned Run over fresh TicketRuntimes through the same `apply` the live Orchestrator
uses, so `resume` continues from the journal and not from any state the dead process held.
`in_flight` and `probing` of the result find the Attempts and Probes that were in flight when the process died.
"""

from typing import TYPE_CHECKING

from factory.journal import EventRow
from factory.pipeline import Pipeline
from factory.run_state import RunMemory, apply

if TYPE_CHECKING:
    from factory.run_state import TicketRuntime


def replay(pipeline: Pipeline, tickets: "list[TicketRuntime]", events: list[EventRow], max_parallel: int) -> RunMemory:
    """Folds `events` over `tickets`, which start out queued, and returns the Run's state."""
    memory = RunMemory(tickets, max_parallel)
    for event in events:
        apply(pipeline, memory, event.type, event.payload)
    return memory
