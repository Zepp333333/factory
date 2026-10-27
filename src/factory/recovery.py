"""Recovery rebuilds what the Orchestrator keeps in memory about a Run from the Run's events.

`replay` plays the events of an orphaned Run over fresh TicketRuntimes, so `resume` continues
from the journal and not from any state the dead process held.
It also finds the Attempts and Probes that were in flight when the process died.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from factory import gates
from factory.artifacts import ProbeDir
from factory.journal import EventRow
from factory.pipeline import TERMINAL_NODES, Pipeline, Stage
from factory.states import SANDBOX_FAILURES, TicketState

if TYPE_CHECKING:
    from factory.orchestrator import TicketRuntime


@dataclass(frozen=True)
class Conflict:
    """A Conflict is the merge of `tip` that `sync` left for `resolve` to edit and the host to commit."""

    tip: str
    files: list[str]


@dataclass
class Replay:
    """A Replay is what the events of a Run say about the parts of the Orchestrator outside its Tickets.

    `in_flight` and `probing` hold the `AttemptStarted` and `ProbeStarted` payloads of the Tickets whose
    Attempt or Probe has no end event.
    `max_parallel` and `round_limits` are what `limits` last set.
    `landings` lists the `(ticket, before, after)` of each merge, and `holder` and `queue` are the Merge window's.
    """

    landings: list[tuple[str, str, str]] = field(default_factory=list)
    holder: str | None = None
    queue: list[str] = field(default_factory=list)
    paused_until: datetime | None = None
    run_waiting: tuple[str, int] | None = None
    run_asked: int = 0
    in_flight: dict[str, dict[str, Any]] = field(default_factory=dict)
    probing: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_parallel: int | None = None
    round_limits: dict[str, int] = field(default_factory=dict)


def replay(pipeline: Pipeline, tickets: "list[TicketRuntime]", events: list[EventRow]) -> Replay:
    """Plays `events` over `tickets`, which start out queued, and returns the rest of the Run's state."""
    by_id = {t.id: t for t in tickets}
    state = Replay()
    for event in events:
        payload = event.payload
        ticket = by_id.get(payload.get("ticket", ""))
        match event.type:
            case "TicketClaimed" if ticket:
                ticket.branch, ticket.base, ticket.synced = payload["branch"], payload["base"], payload["base"]
                ticket.workspace = Path(payload["workspace"])
                ticket.state = TicketState.ACTIVE
            case "NodeEntered" if ticket:
                ticket.node, ticket.gate_attempt = payload["node"], 0
                ticket.cause = payload.get("cause")
                if "round" in payload:
                    ticket.round, ticket.attempt, ticket.spent = payload["round"], 0, 0
                    ticket.rounds[payload["node"]] = payload["round"]
                    ticket.rounds_since_reset[payload["node"]] = payload["rounds_since_reset"]
                if payload["node"] in TERMINAL_NODES and ticket.state == TicketState.ACTIVE:
                    ticket.state = TicketState(payload["node"])
            case "RoundsReset" if ticket:
                for stage in payload["stages"]:
                    ticket.rounds_since_reset[stage] = 0
            case "AttemptStarted" if ticket:
                ticket.attempt = payload["attempt"]
                state.in_flight[ticket.id] = payload
            case "AttemptFinished" if ticket:
                state.in_flight.pop(ticket.id, None)
                _attempt_finished(pipeline, ticket, payload)
            case "AttemptLost" if ticket:
                state.in_flight.pop(ticket.id, None)
                ticket.sandbox_losses += payload["cause"] in SANDBOX_FAILURES
            case "ProbeStarted" if ticket:
                ticket.gate_attempt, ticket.probes[payload["node"]] = payload["attempt"], payload["probe"]
                state.probing[ticket.id] = payload
            case "ProbeInterrupted" if ticket:
                state.probing.pop(ticket.id, None)
            case "ProbeLost" if ticket:
                state.probing.pop(ticket.id, None)
                ticket.sandbox_losses += 1
            case "GateFinished" if ticket:
                state.probing.pop(ticket.id, None)
                ticket.gate_attempt = payload["attempt"]
                ticket.artifacts.append(payload["artifact"])
                if "from" not in payload:
                    ticket.probe_results[payload["node"]] = (payload["probe"], _probe_result(Path(payload["verdict"])))
            case "DecisionWaiting" if payload["ticket"] == "@run":
                state.run_waiting, state.run_asked = (payload["node"], payload["round"]), payload["round"]
            case "DecisionWaiting" if ticket:
                ticket.waiting, ticket.waiting_head = (payload["node"], payload["round"]), payload["head"]
            case "DecisionDecided" | "DecisionOverridden" if payload["ticket"] == "@run":
                state.run_waiting = None
            case "DecisionDecided" | "DecisionOverridden" if ticket:
                ticket.waiting = None
                ticket.answered |= payload["decider"] == "human"
                ticket.artifacts.append(payload["artifact"])
            case "OperationFinished" if ticket:
                _operation_finished(ticket, payload, state)
            case "AbandonedBranchSaved" if ticket:
                ticket.kept = True
            case "MergeWindowQueued":
                state.queue.append(payload["ticket"])
            case "MergeWindowLeft":
                state.queue.remove(payload["ticket"])
            case "TicketCancelled" if ticket:
                ticket.waiting = None
                if ticket.state == TicketState.QUEUED:
                    ticket.state = TicketState.ABANDONED
            case "LimitsChanged":
                state.max_parallel = payload["max_parallel"] or state.max_parallel
                state.round_limits.update(payload["rounds"])
            case "MergeWindowGranted":
                state.queue.remove(payload["ticket"])
                state.holder = payload["ticket"]
            case "MergeWindowReleased":
                state.holder = None
            case "RunPaused":
                state.paused_until = datetime.fromisoformat(payload["resets_at"])
            case "RunResumed":
                state.paused_until = None
    return state


def _attempt_finished(pipeline: Pipeline, ticket: "TicketRuntime", payload: dict[str, Any]) -> None:
    """Applies a finished Attempt: a failed one spends an attempt of the Round unless it cost nothing, a done one leaves
    its Stage output as an artifact and ends the merge a `commits` Stage resolved.
    """
    if payload["outcome"] == "failed":
        cause = payload.get("cause")
        if cause in SANDBOX_FAILURES:
            ticket.sandbox_losses += 1
        elif cause != "rate_limited":
            ticket.spent += 1
        return
    node = pipeline.nodes[payload["node"]]
    if not isinstance(node, Stage):
        return
    ticket.artifacts.append(
        {
            "kind": "stage",
            "node": payload["node"],
            "round": payload["round"],
            "attempt": payload["attempt"],
            "path": payload["output"],
            "head_before": payload["head_before"],
            "head_after": payload["head_after"],
        }
    )
    if node.commits and ticket.conflict is not None:
        ticket.synced, ticket.conflict = ticket.conflict.tip, None


def _operation_finished(ticket: "TicketRuntime", payload: dict[str, Any], state: Replay) -> None:
    if payload["kind"] == "sync" and payload["outcome"] == "clean":
        ticket.synced = payload["before"]
    elif payload["kind"] == "sync":
        files = json.loads(Path(payload["artifact"]).read_text())["files"]
        ticket.conflict = Conflict(payload["before"], files)
        ticket.artifacts.append({"kind": "operation", "node": payload["node"], "path": payload["artifact"]})
    elif payload["outcome"] == "merged":
        ticket.state, ticket.merged_commit = TicketState.MERGED, payload["after"]
        state.landings.append((ticket.id, payload["before"], payload["after"]))


def _probe_result(verdict: Path) -> gates.ProbeResult:
    """Rebuilds the finished Probe a verdict judged, whose reports a Gate with `from` reads."""
    root = ProbeDir(verdict.parent)
    written = json.loads(verdict.read_text())
    return gates.ProbeResult(
        gate_out=root.gate_out,
        log=root.log,
        exit_code=written["exit_code"],
        timed_out=written["exit_code"] is None,
        duration=written["duration"],
    )
