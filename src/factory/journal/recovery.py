"""Recovery rebuilds what the Orchestrator keeps in memory about a Run from the Run's events.

`replay` folds the events of an orphaned Run over fresh TicketRuntimes through the same `apply` the live Orchestrator
uses, so `resume` continues from the journal and not from any state the dead process held.
`in_flight` and `probing` of the result find the Attempts and Probes that were in flight when the process died.
"""

from collections.abc import Sequence

from factory.journal.journal import EventRow
from factory.journal.run_state import RunMemory, TicketRuntime, apply
from factory.pipeline.pipeline import Pipeline
from factory.pipeline.throttle import Brake


def replay(
    pipeline: Pipeline,
    tickets: Sequence[TicketRuntime],
    events: list[EventRow],
    max_parallel: int,
    throttle: dict[str, Brake] | None = None,
    budgets: dict[str, float] | None = None,
    *,
    executor: str,
) -> RunMemory:
    """Folds `events` over `tickets`, which start out queued, and returns the Run's state.

    `throttle` is the brake and `budgets` the budgets the project file sets, which `limits` may have changed since,
    and `executor` is the project's Executor, which a bare window of an old Journal belongs to.
    """
    memory = RunMemory(
        list(tickets), max_parallel, throttle=dict(throttle or {}), budgets=dict(budgets or {}), executor=executor
    )
    for event in events:
        apply(pipeline, memory, event.type, event.payload)
    return memory
