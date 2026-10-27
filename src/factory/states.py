from enum import StrEnum

# An Attempt that fails with one of these causes failed because its Sandbox did, not the agent.
SANDBOX_FAILURES = ("sandbox_start", "sandbox_lost")


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
