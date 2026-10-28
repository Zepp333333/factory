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

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import jsonschema

from factory import deciders
from factory.artifacts import RunDir, write_json_once, write_once
from factory.clock import Clock
from factory.config import Project
from factory.events import Event
from factory.executor import AttemptHandle, AttemptRequest, AttemptResult, Executor, Failure
from factory.git import GitError, HostGit
from factory.journal import DESCRIBER_SLOT, Journal
from factory.pipeline import AgentWork, Decision, Describer, Input, Pipeline, Stage
from factory.run_state import INFRA_FAILURES, RunMemory, RunningAttempt, Silence, TicketRuntime
from factory.sandbox import Mount, Sandbox, SandboxError
from factory.states import OOM, SANDBOX_FAILURES, SANDBOX_LOSSES, SPENT
from factory.workbench import TERMINAL_LOG, AttemptTerminal, Workbench, WorkbenchError

# A rate limit that gives no reset time still in the future pauses new Attempts for this many seconds.
RATE_LIMIT_PAUSE = 600


@dataclass(frozen=True)
class Ports:
    executor: Executor
    sandbox: Sandbox
    workbench: Workbench
    clock: Clock


class Scope(Protocol):
    """A Scope is the Run the Attempts belong to: its id, its directory and the state its events tell."""

    run_id: str
    run_dir: RunDir
    memory: RunMemory


class RefusedHandle:
    """A RefusedHandle stands for an Attempt whose Sandbox did not start the agent."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def poll(self) -> AttemptResult:
        return AttemptResult(output=None, failure="sandbox_start", reason=self._reason)

    def kill(self) -> None:
        pass


@dataclass(frozen=True)
class Ended:
    """Ended is how an Attempt ended, as far as the node it ran for is concerned.

    `outcome` is `done` or `failed` once the node is over, with the `artifact` a done Attempt of a Stage left,
    and None when another Attempt follows.
    `charge` is the counter the Attempt spent, or None when it spent nothing.
    """

    outcome: str | None
    artifact: dict[str, Any] | None = None
    charge: str | None = None


@dataclass(frozen=True)
class CleanupEvents:
    """CleanupEvents say how a clean-up journals each step it ends."""

    removed: Callable[[], None]
    left_behind: Callable[[str], None]
    workspace_removed: Callable[[], None]
    branch_deleted: Callable[[], None]
    failed: Callable[[str, Exception], None]


def attempt_key(ticket: TicketRuntime) -> dict[str, Any]:
    return {"ticket": ticket.id, "node": ticket.node, "round": ticket.round, "attempt": ticket.attempt}


def sandbox_name(run_id: str, ticket: str) -> str:
    return f"{run_id}-{ticket}"


def schema_errors(role_output: Path, output: dict[str, Any] | None) -> list[str]:
    """Returns the ways an output breaks the Role's schema, which is none for no output."""
    if output is None:
        return []
    schema = json.loads(role_output.read_text())
    schema_check = jsonschema.Draft202012Validator(schema)
    return [error.message for error in schema_check.iter_errors(output)]


def resolve_input(run_dir: RunDir, ticket: TicketRuntime, item: Input) -> list[dict[str, Any]]:
    """Returns the Brief entries of one input, which is empty only for an optional input with no artifact yet.

    A Stage's entry carries the commit range of its Round, so the agent reads that code with `git diff`.
    """
    if item.source == "spec":
        return [{"source": "spec", "path": str(run_dir.spec)}]
    if item.source == "ticket":
        return [{"source": "ticket", "path": str(run_dir.ticket(ticket.id))}]
    if item.source == "cause":
        assert ticket.cause is not None, "Config proves that cause has a source on every path"
        return [{"source": "cause", **ticket.cause}]
    found = [a for a in ticket.artifacts if a["kind"] == item.source and a["node"] in item.nodes]
    assert found or item.optional, f"Config proves that {item} has a source on every path"
    picked = found if item.all_rounds else found[-1:]
    return [{"source": item.source, **{k: v for k, v in a.items() if k != "kind"}} for a in picked]


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
        **({"memory_limit": result.memory.limit} if result.memory.limit else {}),
        **({"memory_peak": result.memory.peak} if result.memory.peak is not None else {}),
    }


def gave_back(memory: RunMemory) -> list[dict[str, Any]]:
    """Returns what the Run's Decision gives back of the Attempts in the row of failures.

    Only the Attempts whose Ticket is still in the same Round have anything to give back.
    """
    given = []
    for failed in memory.failures:
        ticket = memory.ticket(failed.ticket)
        assert ticket is not None
        if failed.charge is not None and (ticket.node, ticket.round) == (failed.node, failed.round):
            given.append({"ticket": failed.ticket, "counter": failed.charge})
    return given


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
        scope: Scope,
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
        self.scope = scope
        self.slots = slots
        self.tick = tick
        self.beat = beat
        self.ask_run = ask_run
        self.starved = False

    # Start

    def take_slot(self, holder: str) -> bool:
        """Takes a slot of the machine's cap for the holder's next Attempt, and notes when none is free."""
        if self.journal.take_slot(self.scope.run_id, holder, self.slots):
            return True
        self.starved = True
        return False

    def start(self, ticket: TicketRuntime, node: Stage | Decision) -> bool:
        """Starts the Ticket's next Attempt of the node, or returns False when no slot is free."""
        assert ticket.workspace is not None
        head_before = self.git.head(ticket.workspace, ticket.branch)
        with self.journal.transaction():
            if not self.take_slot(ticket.id):
                return False
            request = self.begin(ticket, node, head_before)
        self.launch(ticket, request, head_before)
        return True

    def begin(self, ticket: TicketRuntime, node: Stage | Decision, head_before: str) -> AttemptRequest:
        """Writes the Attempt's Brief and journals its start, and returns the request the Executor will run.

        The caller holds the slot, and journals this in the transaction that took it.
        """
        assert ticket.workspace is not None
        work = node.agent
        number = ticket.attempt + 1
        attempt_dir = self.scope.run_dir.attempt(ticket.id, work.name, ticket.round, number)
        brief = self._brief(ticket, work, number, attempt_dir)
        brief_path = write_json_once(attempt_dir / "brief.json", brief)
        model = work.model_for(ticket.rounds_since_reset[work.name])
        request = self._request(
            work, model, brief_path, attempt_dir, sandbox_name(self.scope.run_id, ticket.id), ticket.workspace
        )
        self.append(
            Event.AttemptStarted,
            {
                **{**attempt_key(ticket), "attempt": number},
                "model": model,
                "brief_path": str(brief_path),
                "brief": brief,
                "head_before": head_before,
                "stream": str(request.stream),
            },
        )
        return request

    def launch(self, ticket: TicketRuntime, request: AttemptRequest, head_before: str) -> None:
        """Starts the agent in the Ticket's Sandbox and opens the owner's terminal on it.

        A Sandbox that refuses the agent leaves an Attempt that fails with `sandbox_start` on its first poll.
        """
        assert ticket.workspace is not None
        ignored_before = self.git.ignored(ticket.workspace, ticket.branch)
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
            silence=Silence(request.stream, started),
        )

    def _request(
        self, work: AgentWork, model: str, brief: Path, attempt_dir: Path, sandbox: str, workspace: Path
    ) -> AttemptRequest:
        return AttemptRequest(
            stage=work.name,
            model=model,
            profile=work.profile,
            prompt=work.role.prompt,
            brief=brief,
            output_schema=work.role.output,
            attempt_dir=attempt_dir,
            stream=attempt_dir / "stream.ndjson",
            sandbox=sandbox,
            workspace=workspace,
            agent_home=self.scope.run_dir.agent_home,
        )

    def _brief(self, ticket: TicketRuntime, work: AgentWork, attempt: int, attempt_dir: Path) -> dict[str, Any]:
        """Builds the Brief from the node's declared inputs, the Ticket's base, the Release channel and the output."""
        return {
            "run": self.scope.run_id,
            "ticket": ticket.id,
            "stage": work.name,
            "round": ticket.round,
            "attempt": attempt,
            "base": ticket.synced,
            **({"release": self.project.release} if self.project.release else {}),
            "inputs": [entry for item in work.inputs for entry in resolve_input(self.scope.run_dir, ticket, item)],
            **({"outcomes": list(work.outcomes)} if work.outcomes is not None else {}),
            "output_dir": str(attempt_dir),
        }

    def _open_terminal(self, request: AttemptRequest) -> AttemptTerminal | None:
        """Opens the owner's terminal on the Attempt, and goes on without one when the Workbench cannot."""
        try:
            return self.ports.workbench.open_attempt(
                request.stream, request.attempt_dir / TERMINAL_LOG, request.workspace, request.stage
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

    def step(self, ticket: TicketRuntime, node: Stage | Decision, world_lost: Callable[[], bool]) -> Ended | None:
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

    def end(self, ticket: TicketRuntime, node: Stage | Decision, result: AttemptResult) -> Ended:
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
        strays = self._remove_stray_ignored(ticket, running)
        removed = {"removed_ignored": strays} if strays else {}
        if failure is None and work.commits and strays:
            errors = [f"{name} is ignored by .gitignore, so the Stage boundary commit leaves it out" for name in strays]
            failure = "ignored_files"
        if failure is not None:
            record: dict[str, Any] = {
                "outcome": "failed",
                "cause": failure,
                **({"reason": result.reason} if result.reason else {}),
                **({"resets_at": result.resets_at.isoformat()} if result.resets_at else {}),
                **({"silent": result.silent} if result.silent is not None else {}),
                **_memory_record(result),
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
            head_after = self._commit(ticket, work, result.output) or running.head_before
        else:
            discarded = {**self.discard(ticket, running.head_before, attempt_dir), **removed}
            if discarded:
                self.append(Event.WorkspaceViolated, {**attempt_key(ticket), **discarded})
            head_after = running.head_before
        record = {
            "outcome": "done",
            "output": str(output_path),
            "head_before": running.head_before,
            "head_after": head_after,
            **discarded,
        }
        artifact = {
            "kind": "stage",
            "node": work.name,
            "round": ticket.round,
            "attempt": ticket.attempt,
            "path": str(output_path),
            "head_before": running.head_before,
            "head_after": head_after,
        }
        self._record(ticket, attempt_dir, record, {} if work.outcomes is not None else {"artifact": artifact})
        return Ended("done", artifact)

    def _charge(
        self,
        ticket: TicketRuntime,
        work: AgentWork,
        result: AttemptResult,
        failure: Failure,
        attempt_dir: Path,
        record: dict[str, Any],
    ) -> Ended:
        """Journals the failed Attempt with what it charges, and returns how the node goes on.

        An Attempt the Run's Decision holds spends nothing, and one that opens it is asked about.
        """
        held = failure in INFRA_FAILURES and self.scope.memory.opens_decision(
            ticket.id, failure, ticket.node, ticket.round
        )
        opens = held and self.scope.memory.run_waiting is None
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
            if not self.sandbox_gone_too_often(ticket, failure, result.reason):
                return Ended(None, charge=charge)
            return Ended("failed", charge=charge)
        if ticket.spent >= work.attempts:
            return Ended("failed", charge=charge)
        return Ended(None, charge=charge)

    def _pause(self, ticket: TicketRuntime, result: AttemptResult) -> None:
        """Holds back new Attempts of every Ticket until the subscription's limit resets.

        A reset time that is missing or already past gives the fixed pause.
        A pause that already runs longer stays as it is.
        """
        now = self.ports.clock.now()
        until = result.resets_at
        if until is None or until <= now:
            until = now + timedelta(seconds=RATE_LIMIT_PAUSE)
        paused = self.scope.memory.paused_until
        if paused is not None and paused >= until:
            return
        payload = {"resets_at": until.isoformat(), **attempt_key(ticket)}
        self.append(Event.RunPaused, {**payload, **({"reason": result.reason} if result.reason else {})})

    def _record(
        self, ticket: TicketRuntime, attempt_dir: Path, record: dict[str, Any], applied: dict[str, Any]
    ) -> None:
        """Writes the Attempt's record and journals its end with `applied`, what the event does to the Run state."""
        write_json_once(attempt_dir / "attempt.json", record)
        self.append(Event.AttemptFinished, {**attempt_key(ticket), **record, **applied})

    def _remove_stray_ignored(self, ticket: TicketRuntime, running: RunningAttempt) -> list[str]:
        """Removes the ignored files the Attempt created outside the project's `caches`, and returns their names.

        Gates would see them in the Workspace, but `merge` would land the tree without them.
        """
        assert ticket.workspace is not None
        new = self.git.ignored(ticket.workspace, ticket.branch) - running.ignored_before
        strays = sorted(name for name in new if self.project.caches.isdisjoint(Path(name).parts))
        self.git.remove_untracked(ticket.workspace, ticket.branch, strays)
        return strays

    def _commit(self, ticket: TicketRuntime, work: AgentWork, output: dict[str, Any]) -> str | None:
        """Makes the Stage boundary commit and returns it, or None when the Attempt changed nothing."""
        assert ticket.workspace is not None
        summary = str(output.get("summary") or "").strip().partition("\n")[0]
        subject = summary or f"{work.name}: {ticket.id}"
        trailers = {
            "Factory-Run": self.scope.run_id,
            "Factory-Ticket": ticket.id,
            "Factory-Stage": work.name,
            "Factory-Attempt": str(ticket.attempt),
        }
        merging = ticket.conflict.tip if ticket.conflict else None
        commit = self.git.commit_all(ticket.workspace, ticket.branch, subject, trailers, merging)
        if commit is not None:
            self.append(Event.StageBoundaryCommitted, {**attempt_key(ticket), "commit": commit, "subject": subject})
        return commit

    def discard(self, ticket: TicketRuntime, head: str, attempt_dir: Path) -> dict[str, Any]:
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
        ticket: TicketRuntime,
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
            Event.AttemptLost, {**key, "cause": cause, "reason": reason, "mended": mended, "charge": charge, **extra}
        )
        if charge is None or not self.sandbox_gone_too_often(ticket, cause, reason):
            return None
        return Ended("failed", charge=charge)

    def sandbox_gone_too_often(self, ticket: TicketRuntime, cause: str, reason: str | None) -> bool:
        """Tells whether the Ticket lost its Sandbox more often than `sandbox_retries`, and journals it if so.

        An Attempt or a Probe the Sandbox lost counts as a loss of the Ticket, and past the cap its node ends.
        """
        if ticket.sandbox_losses <= self.pipeline.sandbox_retries:
            return False
        self.append(
            Event.SandboxRetriesExhausted,
            {
                "ticket": ticket.id,
                "node": ticket.node,
                "losses": ticket.sandbox_losses,
                "sandbox_retries": self.pipeline.sandbox_retries,
                "cause": cause,
                "reason": reason,
            },
        )
        return True

    # Clean up

    def clean_up(
        self,
        sandbox: str,
        workspace: Path | None,
        branch: str,
        commit: str,
        events: CleanupEvents,
        settle: Callable[[], None] | None = None,
    ) -> None:
        """Removes the Sandbox, the Workspace and the branch an Attempt or a Ticket used, in that order.

        `settle` runs first on a Workspace that is still there, and a `workspace` of None has none to remove.
        A step that fails is reported with its reason and keeps what the steps after it would remove.
        A Sandbox that stays behind stopped for good runs nothing more, so the Workspace and branch still go.
        The branch goes only while it still points at `commit`, so no later commit is lost with it.
        """
        try:
            left_behind = self.ports.sandbox.remove(sandbox)
        except (SandboxError, OSError) as error:
            events.failed("sandbox", error)
            return
        if left_behind is None:
            events.removed()
        else:
            events.left_behind(left_behind)
        if workspace is not None:
            try:
                if settle is not None:
                    settle()
                self.git.pin(workspace, branch)
                self.ports.workbench.remove_workspace(self.project.repo, workspace)
            except (GitError, WorkbenchError, OSError) as error:
                events.failed("workspace", error)
                return
            events.workspace_removed()
        try:
            self.git.delete_branch(branch, commit)
        except GitError as error:
            events.failed("branch", error)
            return
        events.branch_deleted()

    # The Describer

    def describe(
        self,
        describer: Describer,
        attempt: int,
        names: tuple[str, str, Path],
        tip: str,
        brief: Callable[[Path], dict[str, Any]],
        mounts: Callable[[], list[Mount]],
    ) -> AttemptResult:
        """Runs one Attempt of the Describer in a Workspace on a branch at the Integration branch's tip.

        `tip` is the Integration branch's tip.
        The Describer waits for a slot, and its changes are discarded.
        The Workspace, the Sandbox and the branch are removed after it.
        A Sandbox or git step that fails ends the Attempt as `sandbox_start`.
        """
        name, branch, workspace = names
        work = describer.agent
        run_dir = self.scope.run_dir
        attempt_dir = run_dir.describer / f"attempt-{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.git.create_branch(branch, tip)
            self.ports.workbench.create_workspace(self.project.repo, workspace, branch)
            self.ports.sandbox.create(name, workspace, mounts())
            brief_content = brief(attempt_dir)
            brief_path = write_json_once(attempt_dir / "brief.json", brief_content)
            request = self._request(work, work.model, brief_path, attempt_dir, name, workspace)
            started = {
                "attempt": attempt,
                "model": work.model,
                "brief_path": str(brief_path),
                "brief": brief_content,
                "head": tip,
            }
            # The Describer waits for a slot, then takes it and journals its start in one transaction.
            while True:
                with self.journal.transaction():
                    if self.take_slot(DESCRIBER_SLOT):
                        self.append(Event.DescriberStarted, started)
                        break
                self.ports.clock.sleep(self.tick)
                self.beat()
            result = self._run(work, request)
        except (SandboxError, WorkbenchError, GitError, OSError) as error:
            result = AttemptResult(output=None, failure="sandbox_start", reason=str(error))
        self._clean_up_describer(name, workspace, branch, tip, attempt_dir)
        return result

    def _run(self, work: AgentWork, request: AttemptRequest) -> AttemptResult:
        """Waits for the agent of an Attempt that no Ticket owns, which is killed once it runs past the timeout."""
        handle = self.ports.executor.start(request, self.ports.sandbox)
        terminal = self._open_terminal(request)
        started = self.ports.clock.now()
        silence = Silence(request.stream, started)
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
        self.clean_up(name, workspace if workspace.exists() else None, branch, tip, events, settle)
