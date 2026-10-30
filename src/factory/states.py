from enum import StrEnum

# A Ticket that reaches one of these nodes of a Pipeline has left it for good.
TERMINAL_NODES = ("done", "abandoned")

# An Attempt that fails with one of these causes failed because its Sandbox did, not the agent.
SANDBOX_FAILURES = ("sandbox_start", "sandbox_lost")

# An Attempt whose agent the Sandbox's OOM killer killed spends nothing, because it failed on the Sandbox's memory.
OOM = "oom"
OOM_ADVICE = "raise sandbox.memory in the project config"

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
