from enum import StrEnum


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


class CommandState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
