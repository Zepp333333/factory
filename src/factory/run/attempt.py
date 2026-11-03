"""The Attempt module owns one Attempt of a Fresh agent from its start to its end, and decides what it charges.

A Stage, a model Decider and a RunAgent all start their Attempts here: the module takes a Slot, launches the agent
through the Executor in the Sandbox and the Workbench terminal, polls it and kills it on `timeout`, and at the end
checks its output, commits or discards its changes (ADR 0009) and charges it.
The charge is the rule that only the agent's own work spends attempts and Rounds:

- `oom` spends nothing, and a second one in a row opens the Run's Decision, because the same memory fails again;
- `rate_limited` spends nothing and pauses the Run until the limit resets;
- an Attempt held by the Run's Decision `@run` spends nothing, and one that opens the Decision asks the owner;
- `sandbox_start` and `sandbox_lost` count as a loss of the Ticket's Sandbox, and past `sandbox_retries` the node ends;
- every other cause class spends an attempt of the Round.

The charge reaches the Run state only through the events the module journals, so replay sees what the live Run did.
The Orchestrator keeps routing the outcome of the node.
"""

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from factory.config import Project
from factory.core.events import Event, applies, measures, nothing, times
from factory.core.git import HostGit
from factory.core.names import sandbox_name
from factory.core.states import OOM, SANDBOX_FAILURES, SANDBOX_LOSSES, SPENT, Failure
from factory.journal.journal import Journal
from factory.journal.run_state import INFRA_FAILURES, InfraFailure, RunMemory, TicketRuntime
from factory.pipeline import deciders
from factory.pipeline.fingerprint import Variant
from factory.pipeline.pipeline import AgentSettings, AgentWork, Decision, Pipeline, Stage
from factory.ports.executor import AttemptHandle, AttemptRequest, AttemptResult
from factory.ports.sandbox import SandboxError
from factory.ports.workbench import TERMINAL_LOG, AttemptTerminal, WorkbenchError
from factory.run.inputs import resolve_input, schema_errors
from factory.run.isolation import Isolation
from factory.run.live import Ended, LiveTicket, RunningAttempt, Silence, attempt_key
from factory.run.run_context import Ports, RunContext
from factory.views.artifacts import write_json_once, write_once
from factory.views.metrics import FINGERPRINT, MetricsMemory, attempt_metrics, size, spend
from factory.views.timeline import TimelineMemory, duration

# A rate limit that gives no reset time still in the future pauses new Attempts for this many seconds.
RATE_LIMIT_PAUSE = 600


class RefusedHandle:
    """A RefusedHandle stands for an Attempt whose Sandbox did not start the agent."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def poll(self) -> AttemptResult:
        return AttemptResult(output=None, failure="sandbox_start", reason=self._reason)

    def kill(self) -> None:
        pass


def outcome_errors(output: dict[str, Any], outcomes: tuple[str, ...]) -> list[str]:
    """Tells why a model Decider's output is no outcome from the closed list with reasons."""
    errors = []
    if (error := deciders.outcome_error(output.get("outcome"), outcomes)) is not None:
        errors.append(error)
    reasons = output.get("reasons", [])
    if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
        errors.append("reasons must be a list of strings")
    return errors


def _memory_record(result: AttemptResult) -> dict[str, Any]:
    """Returns the Sandbox's memory limit and peak usage that the journal keeps of an Attempt the OOM killer killed."""
    if result.memory is None:
        return {}
    return {
        **({"memory_limit": result.memory.limit} if result.memory.limit is not None else {}),
        **({"memory_peak": result.memory.peak} if result.memory.peak is not None else {}),
    }


class Attempts:
    """Attempts starts, polls, ends and loses the Attempts of a Run.

    `append` journals an event and applies it to the Run state, `beat` keeps the Run's heartbeat while an Attempt
    waits for a Slot or for the agent, and `ask_run` opens the Run's Decision when Attempts of two Tickets fail
    in a row.
    """

    def __init__(
        self,
        journal: Journal,
        append: Callable[[Event, dict[str, Any]], None],
        project: Project,
        pipeline: Pipeline,
        ports: Ports,
        git: HostGit,
        context: RunContext,
        slots: int,
        tick: float,
        beat: Callable[[], None],
        ask_run: Callable[[Failure], None],
    ) -> None:
        self.journal = journal
        self.append = append
        self.project = project
        self.pipeline = pipeline
        self.ports = ports
        self.git = git
        self.context = context
        self.slots = slots
        self.tick = tick
        self.beat = beat
        self.ask_run = ask_run
        self.starved = False
        self.variant = Variant(pipeline, lambda: ports.executor.kit_sha(context.run_dir.skills))
        self.isolation = Isolation(append, project, pipeline, ports, git)

    # Start

    def take_slot(self, holder: str) -> bool:
        """Takes a slot of the machine's cap for the holder's next Attempt, and notes when none is free."""
        if self.journal.take_slot(self.context.run_id, holder, self.slots):
            return True
        self.starved = True
        return False

    def start(self, ticket: LiveTicket, node: Stage | Decision) -> bool:
        """Starts the Ticket's next Attempt of the node, or returns False when no slot is free."""
        assert ticket.workspace is not None
        head_before = self.git.head(ticket.workspace, ticket.branch)
        with self.journal.transaction():
            if not self.take_slot(ticket.id):
                return False
            request = self.begin(ticket, node, head_before)
        self.launch(ticket, request, head_before)
        return True

    def begin(self, ticket: LiveTicket, node: Stage | Decision, head_before: str) -> AttemptRequest:
        """Writes the Attempt's Brief and journals its start, and returns the request the Executor will run.

        The caller holds the slot, and journals this in the transaction that took it.
        """
        assert ticket.workspace is not None
        work = node.agent
        number = ticket.attempt + 1
        attempt_dir = self.context.run_dir.attempt(ticket.id, work.name, ticket.round, number)
        brief = self._brief(ticket, work, number, attempt_dir)
        brief_path = write_json_once(attempt_dir / "brief.json", brief)
        counted = ticket.rounds_since_reset[work.name]
        settings = work.settings.for_round(counted)
        request = self.request(
            work, settings, brief_path, attempt_dir, sandbox_name(self.context.run_id, ticket.id), ticket.workspace
        )
        self.append(
            Event.AttemptStarted,
            {
                **{**attempt_key(ticket), "attempt": number},
                **self.variant.of(work, settings),
                "brief_path": str(brief_path),
                "brief": brief,
                "head_before": head_before,
                "stream": str(request.stream),
            },
        )
        return request

    def launch(self, ticket: LiveTicket, request: AttemptRequest, head_before: str) -> None:
        """Starts the agent in the Ticket's Sandbox and opens the owner's terminal on it.

        A Sandbox that refuses the agent leaves an Attempt that fails with `sandbox_start` on its first poll.
        """
        assert ticket.workspace is not None
        ignored_before = self.git.ignored(ticket.workspace, ticket.branch)
        self.isolation.reap(ticket, request.sandbox)
        handle: AttemptHandle
        try:
            handle = self.ports.executor.start(request, self.ports.sandbox)
        except SandboxError as error:
            handle = RefusedHandle(str(error))
            terminal = None
        else:
            terminal = self._open_terminal(request)
        started = self.ports.clock.now()
        ticket.running = RunningAttempt(
            terminal=terminal,
            handle=handle,
            request=request,
            head_before=head_before,
            started=started,
            ignored_before=ignored_before,
            silence=Silence(request.stream, self.ports.executor, started),
        )

    def request(
        self,
        work: AgentWork,
        settings: AgentSettings,
        brief: Path,
        attempt_dir: Path,
        sandbox: str,
        workspace: Path,
    ) -> AttemptRequest:
        """Returns what the Executor runs for an Attempt of `work` with `settings` in the Sandbox `sandbox`."""
        return AttemptRequest(
            stage=work.name,
            settings=settings,
            prompt=work.role.prompt,
            brief=brief,
            output_schema=work.role.output,
            attempt_dir=attempt_dir,
            stream=attempt_dir / "stream.ndjson",
            sandbox=sandbox,
            workspace=workspace,
            agent_home=self.context.run_dir.agent_home,
            kit=self.context.run_dir.kit,
        )

    def _brief(self, ticket: LiveTicket, work: AgentWork, attempt: int, attempt_dir: Path) -> dict[str, Any]:
        """Builds the Brief from the node's declared inputs, the Ticket's base, the Release channel and the output."""
        return {
            "run": self.context.run_id,
            "ticket": ticket.id,
            "stage": work.name,
            "round": ticket.round,
            "attempt": attempt,
            "base": ticket.synced,
            **({"release": self.project.release} if self.project.release else {}),
            "inputs": [entry for item in work.inputs for entry in resolve_input(self.context.run_dir, ticket, item)],
            **({"outcomes": list(work.outcomes)} if work.outcomes is not None else {}),
            "output_dir": str(attempt_dir),
        }

    def _open_terminal(self, request: AttemptRequest) -> AttemptTerminal | None:
        """Opens the owner's terminal on the Attempt, and goes on without one when the Workbench cannot."""
        try:
            return self.ports.workbench.open_attempt(
                request.stream,
                request.attempt_dir / TERMINAL_LOG,
                request.workspace,
                request.stage,
                self.ports.executor.name,
            )
        except (WorkbenchError, OSError):
            return None

    # Poll

    def _poll(
        self, handle: AttemptHandle, started: datetime, silence: Silence, work: AgentWork
    ) -> AttemptResult | None:
        """Polls the agent and kills it once it runs past the timeout of its work, or is silent past its stall."""
        result = handle.poll()
        if result is not None:
            return result
        now = self.ports.clock.now()
        silent = silence.watch(now)
        settings = work.settings
        if (now - started).total_seconds() >= settings.timeout:
            handle.kill()
            return AttemptResult(output=None, failure="timeout", reason=f"no result after {settings.timeout:g}s")
        if settings.stall is not None and silent > settings.stall:
            handle.kill()
            return AttemptResult(
                output=None,
                failure="stalled",
                reason=f"no progress for {silent:g}s, past the stall of {settings.stall:g}s",
                silent=silent,
            )
        return None

    def step(self, ticket: LiveTicket, node: Stage | Decision, world_lost: Callable[[], bool]) -> Ended | None:
        """Polls the Ticket's running Attempt and ends it once it has a result.

        Returns None while the agent works.
        A failed Attempt first asks `world_lost` whether the Ticket's world is gone, which loses the Attempt instead of
        charging it, and then another Attempt follows.
        """
        assert ticket.running is not None
        result = self._poll(ticket.running.handle, ticket.running.started, ticket.running.silence, node.agent)
        if result is None:
            return None
        if result.failure is not None and world_lost():
            return Ended(None)
        return self.end(ticket, node, result)

    def kill(self, running: RunningAttempt) -> None:
        """Stops the agent of a running Attempt and closes its terminal."""
        running.handle.kill()
        self._close_terminal(running.terminal)

    def _close_terminal(self, terminal: AttemptTerminal | None) -> None:
        if terminal is not None:
            terminal.close()

    # End

    def end(self, ticket: LiveTicket, node: Stage | Decision, result: AttemptResult) -> Ended:
        """Records how the Attempt ended and charges it, and returns how the node goes on.

        A failed Attempt leaves its changes in `discarded.patch`, and the Workspace goes back to `head_before`.
        A done Attempt of a `commits` node gets its Stage boundary commit.
        A done Attempt of a read-only node has its changes discarded the same way, and the journal records it.
        A node with `outcomes` is a model Decider's, and an output that names none of them fails it.
        """
        assert ticket.running is not None and ticket.workspace is not None
        work = node.agent
        running, ticket.running = ticket.running, None
        self._close_terminal(running.terminal)
        attempt_dir = running.request.attempt_dir
        errors = schema_errors(work.role.output, result.output)
        if not errors and work.outcomes is not None and result.output is not None:
            errors = outcome_errors(result.output, work.outcomes)

        # The Executor holds the agent to the Role schema, so output that breaks it means the Executor broke.
        failure: Failure | None = result.failure or ("executor_crash" if errors else None)
        if failure is None and work.commits and ticket.conflict is not None:
            left = self.git.markers(ticket.workspace, ticket.conflict.files)
            errors = [f"{name} still holds conflict markers" for name in left]
            failure = "conflict_markers" if errors else None
        spent = {
            "duration": (self.ports.clock.now() - running.started).total_seconds(),
            "usage": asdict(result.usage) if result.usage else None,
            "stats": asdict(result.stats) if result.stats else None,
        }
        strays = self._remove_stray_ignored(ticket, running)
        removed = {"removed_ignored": strays} if strays else {}
        if failure is not None:
            record: dict[str, Any] = {
                "outcome": "failed",
                "cause": failure,
                **({"reason": result.reason} if result.reason else {}),
                **({"resets_at": result.resets_at.isoformat()} if result.resets_at else {}),
                **({"silent": result.silent} if result.silent is not None else {}),
                **_memory_record(result),
                **spent,
                "errors": errors,
                "head_before": running.head_before,
                **self.discard(ticket, running.head_before, attempt_dir),
                **removed,
            }
            return self._charge(ticket, work, result, failure, attempt_dir, record)

        assert result.output is not None
        output_path = write_json_once(attempt_dir / "output.json", result.output)
        if work.commits:
            discarded = {}
            kept = removed
            head_after = self._commit(ticket, work, result.output) or running.head_before
        else:
            discarded = {**self.discard(ticket, running.head_before, attempt_dir), **removed}
            kept = {}
            if discarded:
                self.append(Event.WorkspaceViolated, {**attempt_key(ticket), **discarded})
            head_after = running.head_before
        record = {
            "outcome": "done",
            "output": str(output_path),
            "head_before": running.head_before,
            "head_after": head_after,
            **spent,
            **discarded,
            **kept,
        }
        artifact = {
            "kind": "stage",
            "node": work.name,
            "round": ticket.round,
            "attempt": ticket.attempt,
            "path": str(output_path),
            "head_before": running.head_before,
            "head_after": head_after,
            **kept,
        }
        self._record(ticket, attempt_dir, record, {} if work.outcomes is not None else {"artifact": artifact})
        return Ended("done", artifact)

    def _charge(
        self,
        ticket: LiveTicket,
        work: AgentWork,
        result: AttemptResult,
        failure: Failure,
        attempt_dir: Path,
        record: dict[str, Any],
    ) -> Ended:
        """Journals the failed Attempt with what it charges, and returns how the node goes on.

        An Attempt the Run's Decision holds spends nothing, and one that opens it is asked about.
        """
        held = failure in INFRA_FAILURES and self.context.memory.opens_decision(
            ticket.id, failure, ticket.node, ticket.round
        )
        opens = held and self.context.memory.run_waiting is None
        charge = None
        if failure not in ("rate_limited", OOM) and not held:
            charge = SANDBOX_LOSSES if failure in SANDBOX_FAILURES else SPENT
        self._record(ticket, attempt_dir, record, {"charge": charge})
        if failure == "rate_limited":
            self._pause(ticket, result)
        elif held:
            if opens:
                self.ask_run(failure)
            return Ended(None, charge=charge)
        elif charge == SANDBOX_LOSSES:
            if not self.isolation.sandbox_gone_too_often(ticket, failure, result.reason):
                return Ended(None, charge=charge)
            return Ended("failed", charge=charge)
        if ticket.spent >= work.settings.attempts:
            return Ended("failed", charge=charge)
        return Ended(None, charge=charge)

    def _pause(self, ticket: LiveTicket, result: AttemptResult) -> None:
        """Holds back new Attempts of every Ticket until the subscription's limit resets.

        A reset time that is missing or already past gives the fixed pause.
        A pause that already runs longer stays as it is.
        """
        now = self.ports.clock.now()
        until = result.resets_at
        if until is None or until <= now:
            until = now + timedelta(seconds=RATE_LIMIT_PAUSE)
        paused = self.context.memory.paused_until
        if paused is not None and paused >= until:
            return
        payload = {"resets_at": until.isoformat(), **attempt_key(ticket)}
        self.append(Event.RunPaused, {**payload, **({"reason": result.reason} if result.reason else {})})

    def _record(self, ticket: LiveTicket, attempt_dir: Path, record: dict[str, Any], applied: dict[str, Any]) -> None:
        """Writes the Attempt's record and journals its end with `applied`, what the event does to the Run state."""
        write_json_once(attempt_dir / "attempt.json", record)
        self.append(Event.AttemptFinished, {**attempt_key(ticket), **record, **applied})

    def _remove_stray_ignored(self, ticket: LiveTicket, running: RunningAttempt) -> list[str]:
        """Removes the ignored files the Attempt created outside the project's `caches`, and returns their names.

        Gates would see them in the Workspace, but `merge` would land the tree without them.
        """
        assert ticket.workspace is not None
        new = self.git.ignored(ticket.workspace, ticket.branch) - running.ignored_before
        strays = sorted(name for name in new if self.project.caches.isdisjoint(Path(name).parts))
        self.git.remove_untracked(ticket.workspace, ticket.branch, strays)
        return strays

    def _commit(self, ticket: LiveTicket, work: AgentWork, output: dict[str, Any]) -> str | None:
        """Makes the Stage boundary commit and returns it, or None when the Attempt changed nothing."""
        assert ticket.workspace is not None
        summary = str(output.get("summary") or "").strip().partition("\n")[0]
        subject = summary or f"{work.name}: {ticket.id}"
        trailers = {
            "Factory-Run": self.context.run_id,
            "Factory-Ticket": ticket.id,
            "Factory-Stage": work.name,
            "Factory-Attempt": str(ticket.attempt),
        }
        merging = ticket.conflict.tip if ticket.conflict else None
        commit = self.git.commit_all(ticket.workspace, ticket.branch, subject, trailers, merging)
        if commit is not None:
            # The first parent of the commit is the head it was made on, the Ticket branch's head before it.
            size = self.git.size(f"{commit}^", commit)
            payload = {**attempt_key(ticket), "commit": commit, "subject": subject, **size}
            self.append(Event.StageBoundaryCommitted, payload)
        return commit

    def discard(self, ticket: LiveTicket, head: str, attempt_dir: Path) -> dict[str, Any]:
        """Resets the Workspace to `head` and keeps what the Attempt left there in `discarded.patch`."""
        assert ticket.workspace is not None
        patch, files = self.git.discard(ticket.workspace, ticket.branch, head)
        if ticket.conflict is not None:
            # The merge is what `resolve` works on, so the next Attempt starts from the same conflict.
            self.git.begin_merge(ticket.workspace, ticket.branch, ticket.conflict.tip)
        if not patch:
            return {}
        path = write_once(attempt_dir / "discarded.patch", patch)
        return {"discarded": str(path), "files": files}

    # Loss

    def lose(
        self,
        ticket: LiveTicket,
        attempt: dict[str, Any],
        cause: str,
        reason: str,
        mended: list[str],
        extra: dict[str, Any],
    ) -> Ended | None:
        """Journals that the Attempt `attempt` (its key and `head_before`) is lost, and spends none of the Stage's.

        Another Attempt follows on the next tick.
        A lost Sandbox still counts toward the Ticket's `sandbox_retries`, and past it the node ends as if every Attempt
        of its Round failed, which the returned Ended tells.
        """
        key = {k: attempt[k] for k in ("ticket", "node", "round", "attempt", "head_before")}
        charge = SANDBOX_LOSSES if cause in SANDBOX_FAILURES else None
        self.append(
            Event.AttemptLost,
            # A lost Attempt gave no `result`, so its usage is unknown and not zero.
            {
                **key,
                "cause": cause,
                "reason": reason,
                "mended": mended,
                "charge": charge,
                "usage": None,
                "stats": None,
                **extra,
            },
        )
        if charge is None or not self.isolation.sandbox_gone_too_often(ticket, cause, reason):
            return None
        return Ended("failed", charge=charge)

    # Apart from any Ticket

    def run_apart(self, work: AgentWork, request: AttemptRequest) -> AttemptResult:
        """Runs the agent of an Attempt that no Ticket owns and waits for it, killing it once it runs past the timeout.

        The caller holds a slot for it, and `run_agents` says how a RunAgent takes one.
        """
        handle = self.ports.executor.start(request, self.ports.sandbox)
        terminal = self._open_terminal(request)
        started = self.ports.clock.now()
        silence = Silence(request.stream, self.ports.executor, started)
        try:
            while (result := self._poll(handle, started, silence, work)) is None:
                self.ports.clock.sleep(self.tick)
                self.beat()
        finally:
            self._close_terminal(terminal)
        return result


# What the events of an Attempt tell the readers of the Journal.

applies(Event.StageBoundaryCommitted, Event.WorkspaceViolated)(nothing)


@applies(Event.AttemptStarted)
def _apply_attempt_started(memory: RunMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is not None:
        ticket.attempt = payload["attempt"]
        memory.in_flight[ticket.id] = payload


@applies(Event.AttemptFinished)
def _apply_attempt_finished(memory: RunMemory, payload: dict[str, Any]) -> None:
    """Applies a finished Attempt: a failed one charges what its `charge` names and joins the row of failures,
    a done one ends the row, leaves its Stage output as an artifact and ends the merge a `commits` Stage resolved.
    """
    if (ticket := memory.ticket_of(payload)) is None:
        return
    memory.in_flight.pop(ticket.id, None)
    if payload["outcome"] == "failed":
        charge = payload.get("charge")
        _charge(ticket, charge)
        if payload["cause"] in INFRA_FAILURES:
            if memory.run_waiting is None:
                failure = InfraFailure(ticket.id, payload["cause"], payload["node"], payload["round"], charge)
                memory.failures = memory.row_with(failure)
        elif payload["cause"] != "rate_limited":
            memory.failures = []
        return
    memory.failures = []
    if "artifact" in payload:
        ticket.artifacts.append(payload["artifact"])
    if memory.commits(payload["node"]) and ticket.conflict is not None:
        ticket.synced, ticket.conflict = ticket.conflict.tip, None


@applies(Event.AttemptLost)
def _apply_attempt_lost(memory: RunMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is not None:
        memory.in_flight.pop(ticket.id, None)
        _charge(ticket, payload.get("charge"))


@applies(Event.RunPaused)
def _apply_run_paused(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.paused_until = datetime.fromisoformat(payload["resets_at"])


def _charge(ticket: TicketRuntime, counter: str | None) -> None:
    if counter == SPENT:
        ticket.spent += 1
    elif counter == SANDBOX_LOSSES:
        ticket.sandbox_losses += 1


@times(Event.AttemptStarted)
def _time_attempt_started(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    if (entry := memory.entry(payload)) is not None:
        entry.started = memory.at
        entry.attempt = payload.get("attempt")


@times(Event.AttemptFinished)
def _time_attempt_finished(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    if (entry := memory.entry(payload)) is None:
        return
    if entry.started is not None:
        entry.seconds += (memory.at - entry.started).total_seconds()
    entry.outcome = payload["outcome"]
    entry.details = [payload["cause"]] if "cause" in payload else []
    if "silent" in payload:
        entry.details.append(f"silent {duration(payload['silent'])}")
    if usage := payload.get("usage"):
        entry.turns = (entry.turns or 0) + usage["turns"]
        entry.cost = (entry.cost or 0) + usage["cost_usd"]
        memory.total_cost += usage["cost_usd"]


@measures(Event.AttemptStarted)
def _measure_attempt_started(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is None:
        return
    node = payload["node"]
    ticket.attempts[node] = ticket.attempts.get(node, 0) + 1
    waited = memory.idle.pop(ticket.ticket, None)
    if waited is not None and waited[0] == node:
        memory.gaps.setdefault(ticket.ticket, []).append((waited[1], memory.at))
    key = (ticket.ticket, node, payload["round"], payload["attempt"])
    memory.fingerprints[key] = {name: payload.get(name) for name in FINGERPRINT}
    ticket.fingerprints.setdefault(node, memory.fingerprints[key])


@measures(Event.AttemptFinished)
def _measure_attempt_finished(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is None:
        return
    node = payload["node"]
    key = (ticket.ticket, node, payload["round"], payload["attempt"])
    memory.attempts.append(attempt_metrics(ticket, payload, memory.fingerprints.get(key, {})))
    spend(ticket, payload)
    ticket.work_s += payload.get("duration") or 0
    if payload["outcome"] == "failed":
        cause = payload.get("cause") or "unknown"
        ticket.failures[cause] = ticket.failures.get(cause, 0) + 1
        memory.idle[ticket.ticket] = (node, memory.at)
    stats = payload.get("stats") or {}
    for window, used in (stats.get("utilization_start") or {}).items():
        memory.first_use.setdefault(window, used)
    for window, used in (stats.get("utilization_end") or {}).items():
        memory.last_use[window] = used


@measures(Event.AttemptLost)
def _measure_attempt_lost(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is not None:
        ticket.cost_unknown += 1
        ticket.failures[payload["cause"]] = ticket.failures.get(payload["cause"], 0) + 1
        memory.idle[ticket.ticket] = (payload["node"], memory.at)


@measures(Event.StageBoundaryCommitted)
def _measure_stage_boundary_committed(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is not None and "files_changed" in payload:
        ticket.stage_changes.append(size(payload["node"], payload))


@measures(Event.WorkspaceViolated)
def _measure_workspace_violated(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is not None:
        ticket.workspace_violations += 1


@measures(Event.RunPaused)
def _measure_run_paused(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if memory.paused_since is None:
        memory.paused_since = memory.at
