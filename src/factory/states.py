from enum import StrEnum

# An Attempt that fails with one of these causes failed because its Sandbox did, not the agent.
SANDBOX_FAILURES = ("sandbox_start", "sandbox_lost")

# The counters of a Ticket that a failed or lost Attempt can charge: the Round's `spent` and the Ticket's losses.
SPENT = "spent"
SANDBOX_LOSSES = "sandbox_losses"


class TicketState(StrEnum):
    QUEUED = "queued"
    ACTIVE = "active"
    MERGED = "merged"
    DONE = "done"
    ABANDONED = "abandoned"


class RunState(StrEnum):
    RUNNING = "running"
    DONE = "done"
    PARTIAL = "partial"
    ABORTED = "aborted"
    ORPHANED = "orphaned"


class CommandState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
