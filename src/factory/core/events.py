"""The Event names every module of the factory uses for what a Run's journal records, and the readers' handlers of each.

A name is defined here once, and no other module spells one as a string.
A reader of the Journal folds events through a registry of handlers, one per event name: `applies` for the Run state
of `run_state`, `times` for the lines of `timeline` and `measures` for the numbers of `metrics`.
The module that writes an event registers its handlers next to the code that writes it, so a new event touches its
writer and this enum and no reader.
A handler takes the reader's memory and the event's payload and nothing else.
The handlers register when their writer is imported, and `wiring` imports every writer.
"""

from collections.abc import Callable
from enum import StrEnum
from typing import Any

type Handler = Callable[[Any, dict[str, Any]], None]


class Event(StrEnum):
    """Event is the type of a journal event, which is also its name."""

    RunStarted = "RunStarted"
    SpecSnapshotted = "SpecSnapshotted"
    TicketsSelected = "TicketsSelected"
    SpecRendered = "SpecRendered"
    IntegrationBranchCreated = "IntegrationBranchCreated"
    RunNodeEntered = "RunNodeEntered"
    RunNodeEnded = "RunNodeEnded"
    RunReclaimed = "RunReclaimed"
    RunOrphaned = "RunOrphaned"
    Heartbeat = "Heartbeat"
    RunPaused = "RunPaused"
    RunResumed = "RunResumed"
    ThrottleOn = "ThrottleOn"
    ThrottleOff = "ThrottleOff"
    RunFinished = "RunFinished"
    SleepBlocked = "SleepBlocked"
    SleepBlockUnavailable = "SleepBlockUnavailable"
    LimitsChanged = "LimitsChanged"
    TicketClaimed = "TicketClaimed"
    TicketCancelled = "TicketCancelled"
    TicketBranchDeleted = "TicketBranchDeleted"
    NodeEntered = "NodeEntered"
    RoundsReset = "RoundsReset"
    RoundsExhausted = "RoundsExhausted"
    AttemptStarted = "AttemptStarted"
    AttemptFinished = "AttemptFinished"
    AttemptLost = "AttemptLost"
    WorldMended = "WorldMended"
    WorkspaceViolated = "WorkspaceViolated"
    StageBoundaryCommitted = "StageBoundaryCommitted"
    ProbeStarted = "ProbeStarted"
    ProbeInterrupted = "ProbeInterrupted"
    ProbeLost = "ProbeLost"
    GateFinished = "GateFinished"
    SandboxRetriesExhausted = "SandboxRetriesExhausted"
    OperationFinished = "OperationFinished"
    ConflictDropped = "ConflictDropped"
    IssueFiled = "IssueFiled"
    DecisionWaiting = "DecisionWaiting"
    DecisionDecided = "DecisionDecided"
    DecisionOverridden = "DecisionOverridden"
    OwnerEditsCommitted = "OwnerEditsCommitted"
    CommandAccepted = "CommandAccepted"
    CommandRejected = "CommandRejected"
    MergeWindowQueued = "MergeWindowQueued"
    MergeWindowGranted = "MergeWindowGranted"
    MergeWindowReleased = "MergeWindowReleased"
    MergeWindowLeft = "MergeWindowLeft"
    AbandonedBranchSaved = "AbandonedBranchSaved"
    AbandonedWithoutWorkspace = "AbandonedWithoutWorkspace"
    AbandonedWithoutWork = "AbandonedWithoutWork"
    UnsavedWorkCommitted = "UnsavedWorkCommitted"
    SandboxStopFailed = "SandboxStopFailed"
    SandboxStartFailed = "SandboxStartFailed"
    SandboxProcessKilled = "SandboxProcessKilled"
    SandboxReapFailed = "SandboxReapFailed"
    SandboxRemoved = "SandboxRemoved"
    SandboxLeftBehind = "SandboxLeftBehind"
    WorkspaceRemoved = "WorkspaceRemoved"
    CleanupFailed = "CleanupFailed"
    LandingFailed = "LandingFailed"
    IntegrationBranchPushed = "IntegrationBranchPushed"
    PullRequestOpened = "PullRequestOpened"
    PullRequestRecorded = "PullRequestRecorded"
    PullRequestDescribed = "PullRequestDescribed"
    CiStarted = "CiStarted"
    CiFinished = "CiFinished"
    CiAbsent = "CiAbsent"
    TrackerWaitStarted = "TrackerWaitStarted"
    TrackerWaitEnded = "TrackerWaitEnded"
    FixStarted = "FixStarted"
    FixCommitted = "FixCommitted"
    FixGateFinished = "FixGateFinished"
    FixFinished = "FixFinished"
    RunAgentSandboxRemoved = "RunAgentSandboxRemoved"
    RunAgentSandboxLeftBehind = "RunAgentSandboxLeftBehind"
    RunAgentCleanupFailed = "RunAgentCleanupFailed"

    # Older factories journaled these, which the factory reads and no longer writes.
    CiFixStarted = "CiFixStarted"
    CiFixCommitted = "CiFixCommitted"
    CiFixGateFinished = "CiFixGateFinished"
    CiFixFinished = "CiFixFinished"
    CiFixSandboxRemoved = "CiFixSandboxRemoved"
    CiFixSandboxLeftBehind = "CiFixSandboxLeftBehind"
    CiFixCleanupFailed = "CiFixCleanupFailed"
    DescriberSandboxRemoved = "DescriberSandboxRemoved"
    DescriberSandboxLeftBehind = "DescriberSandboxLeftBehind"
    DescriberCleanupFailed = "DescriberCleanupFailed"
    PlannerSandboxRemoved = "PlannerSandboxRemoved"
    PlannerSandboxLeftBehind = "PlannerSandboxLeftBehind"
    PlannerCleanupFailed = "PlannerCleanupFailed"

    PullRequestEdited = "PullRequestEdited"
    PullRequestMerged = "PullRequestMerged"
    PullRequestMergeRefused = "PullRequestMergeRefused"
    PullRequestEditFailed = "PullRequestEditFailed"
    DescriberStarted = "DescriberStarted"
    DescriberFailed = "DescriberFailed"
    PlannerStarted = "PlannerStarted"
    PlannerFinished = "PlannerFinished"
    PlannerFailed = "PlannerFailed"
    TrackerActed = "TrackerActed"
    TrackerActionFailed = "TrackerActionFailed"
    TrackerDelivered = "TrackerDelivered"
    TrackerDeliveryFailed = "TrackerDeliveryFailed"


class Registry:
    """A Registry maps the name of an event to the one handler that tells the reader `reads` what the event does.

    It takes one handler per event and rejects a second one, so two writers cannot both say what one event does.
    An event the Registry `skips` is one the reader leaves out of its events altogether.
    """

    def __init__(self, reads: str) -> None:
        self.reads = reads
        self._handlers: dict[str, Handler] = {}
        self._skipped: set[str] = set()

    def __call__[H: Handler](self, *types: Event) -> Callable[[H], H]:
        """Registers the decorated function as the handler of each of `types`."""

        def register(handler: H) -> H:
            for type in types:
                self._take(type)
                self._handlers[type] = handler
            return handler

        return register

    def skip(self, *types: Event) -> None:
        """Tells the reader to leave each of `types` out of its events, as if the Journal did not hold it."""
        for type in types:
            self._take(type)
            self._skipped.add(type)

    def skips(self, type: str) -> bool:
        return type in self._skipped

    def get(self, type: str) -> Handler | None:
        """Returns the handler of the event `type`, or None for an event the reader takes no notice of."""
        return self._handlers.get(type)

    def __contains__(self, type: str) -> bool:
        return type in self._handlers

    def _take(self, type: Event) -> None:
        if type in self._handlers or type in self._skipped:
            raise ValueError(f"{self.reads} has a handler for {type} already")


def nothing(memory: Any, payload: dict[str, Any]) -> None:
    """Is the handler of an event that tells the reader nothing, which its writer registers to say so."""


applies = Registry("run_state")
times = Registry("timeline")
measures = Registry("metrics")
