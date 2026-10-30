from enum import StrEnum
from typing import Literal

# Every failed Attempt carries one of these cause classes.
# `agent_config` means the agent loaded more or less than the factory gave it.
# `rate_limited` means the subscription refused the agent until `resets_at`.
# `sandbox_lost` means the Sandbox died under the agent.
# `conflict_markers` means the agent left conflict markers in files that `sync` left in conflict.
# `ignored_files` means the agent wrote files that `.gitignore` keeps out of the Stage boundary commit.
# `stalled` means the agent made no progress for longer than the Stage's `stall`.
# `oom` means the Sandbox's OOM killer killed the agent, which a retry in the same memory is likely to repeat.
Failure = Literal[
    "auth",
    "sandbox_start",
    "sandbox_lost",
    "executor_crash",
    "timeout",
    "stalled",
    "oom",
    "agent_config",
    "rate_limited",
    "conflict_markers",
    "ignored_files",
]

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
