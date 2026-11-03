"""The Attempt module owns one Attempt of a Fresh agent from its start to its end, and decides what it charges.

A Stage, a model Decider and the Describer all start their Attempts here: the module takes a Slot, launches the agent
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

from factory import deciders
from factory.artifacts import write_json_once, write_once
from factory.config import Project
from factory.events import Event
from factory.executor import AttemptHandle, AttemptRequest, AttemptResult
from factory.fingerprint import Variant
from factory.git import GitError, HostGit
from factory.inputs import resolve_input, schema_errors
from factory.isolation import CleanupEvents, Isolation
from factory.journal import DESCRIBER_SLOT, Journal
from factory.live import Ended, LiveTicket, RunningAttempt, Silence, attempt_key
from factory.names import sandbox_name
from factory.pipeline import AgentWork, Decision, Describer, Pipeline, Stage
from factory.run_context import Ports, RunContext
from factory.run_state import INFRA_FAILURES
from factory.sandbox import Mount, SandboxError
from factory.states import OOM, SANDBOX_FAILURES, SANDBOX_LOSSES, SPENT, Failure
from factory.workbench import TERMINAL_LOG, AttemptTerminal, WorkbenchError

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
        model, effort = work.model_for(counted), work.effort_for(counted)
        request = self._request(
            work, model, effort, brief_path, attempt_dir, sandbox_name(self.context.run_id, ticket.id), ticket.workspace
        )
        self.append(
            Event.AttemptStarted,
            {
                **{**attempt_key(ticket), "attempt": number},
                **self.variant.of(work, model, effort),
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

    def _request(
        self,
        work: AgentWork,
        model: str,
        effort: str | None,
        brief: Path,
        attempt_dir: Path,
        sandbox: str,
        workspace: Path,
    ) -> AttemptRequest:
        return AttemptRequest(
            stage=work.name,
            model=model,
            effort=effort,
            profile=work.profile,
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
        if (now - started).total_seconds() >= work.timeout:
            handle.kill()
            return AttemptResult(output=None, failure="timeout", reason=f"no result after {work.timeout:g}s")
        if work.stall is not None and silent > work.stall:
            handle.kill()
            return AttemptResult(
                output=None,
                failure="stalled",
                reason=f"no progress for {silent:g}s, past the stall of {work.stall:g}s",
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
        if ticket.spent >= work.attempts:
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

    # The Describer

    def describe(
        self,
        describer: Describer,
        attempt: int,
        names: tuple[str, str, Path],
        tip: str,
        brief: Callable[[Path], dict[str, Any]],
        mounts: Callable[[], list[Mount]],
        layer: int | None = None,
    ) -> AttemptResult:
        """Runs one Attempt of the Describer in a Workspace on a branch at the Integration branch's tip.

        `tip` is the Integration branch's tip, or the commit of layer `layer` of a stacked Run, whose Attempts have
        a directory of their own.
        The Describer waits for a slot, and its changes are discarded.
        The Workspace, the Sandbox and the branch are removed after it.
        A Sandbox or git step that fails ends the Attempt as `sandbox_start`.
        """
        name, branch, workspace = names
        work = describer.agent
        run_dir = self.context.run_dir
        attempt_dir = run_dir.describer / (f"layer-{layer}" if layer else "") / f"attempt-{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.git.create_branch(branch, tip)
            self.ports.workbench.create_workspace(self.project.repo, workspace, branch)
            self.ports.sandbox.create(name, workspace, mounts())
            started = {"attempt": attempt, "model": work.model, "effort": work.effort, "head": tip}
            result = self.work_apart(
                work,
                work.model,
                work.effort,
                name,
                workspace,
                attempt_dir,
                brief(attempt_dir),
                DESCRIBER_SLOT,
                Event.DescriberStarted,
                started,
            )
        except (SandboxError, WorkbenchError, GitError, OSError) as error:
            result = AttemptResult(output=None, failure="sandbox_start", reason=str(error))
        self._clean_up_describer(name, workspace, branch, tip, attempt_dir)
        return result

    def work_apart(
        self,
        work: AgentWork,
        model: str,
        effort: str | None,
        name: str,
        workspace: Path,
        attempt_dir: Path,
        brief: dict[str, Any],
        holder: str,
        event: Event,
        started: dict[str, Any],
    ) -> AttemptResult:
        """Runs an Attempt that no Ticket owns in the Sandbox `name` and returns its result.

        It writes the Brief, waits for a slot of `holder`, takes it and journals `event` with `started` and the Brief in
        one transaction, and waits for the agent.
        The caller journals the event that ends the Attempt and gives the slot back.
        """
        brief_path = write_json_once(attempt_dir / "brief.json", brief)
        request = self._request(work, model, effort, brief_path, attempt_dir, name, workspace)
        payload = {**started, "brief_path": str(brief_path), "brief": brief}
        while True:
            with self.journal.transaction():
                if self.take_slot(holder):
                    self.append(event, payload)
                    break
            self.ports.clock.sleep(self.tick)
            self.beat()
        return self._run(work, request)

    def _run(self, work: AgentWork, request: AttemptRequest) -> AttemptResult:
        """Waits for the agent of an Attempt that no Ticket owns, which is killed once it runs past the timeout."""
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

    def _clean_up_describer(self, name: str, workspace: Path, branch: str, tip: str, attempt_dir: Path) -> None:
        """Removes what the Describer's Attempt used, and keeps what its agent changed in `discarded.patch`.

        Each step that ends is journaled, and a step that fails is journaled with its reason and leaves the steps
        after it undone.
        """

        def settle() -> None:
            patch, _ = self.git.discard(workspace, branch, tip)
            if patch:
                write_once(attempt_dir / "discarded.patch", patch)

        def noop() -> None:
            pass

        events = CleanupEvents(
            removed=lambda: self.append(Event.DescriberSandboxRemoved, {"sandbox": name}),
            left_behind=lambda reason: self.append(
                Event.DescriberSandboxLeftBehind, {"sandbox": name, "reason": reason}
            ),
            workspace_removed=noop,
            branch_deleted=noop,
            failed=lambda step, error: self.append(Event.DescriberCleanupFailed, {"reason": str(error)}),
        )
        self.isolation.clean_up(name, workspace if workspace.exists() else None, branch, tip, events, settle)
