"""The Orchestrator is the deterministic loop that drives one Run and is the only writer of its events."""

import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema

from factory import gates
from factory.artifacts import ProbeDir, RunDir, write_json_once, write_once
from factory.clock import Clock
from factory.config import TERMINAL_NODES, Gate, Operation, Project, Stage
from factory.executor import AttemptHandle, AttemptRequest, AttemptResult, Executor
from factory.git import HostGit
from factory.journal import Journal
from factory.run_view import RunView
from factory.sandbox import Process, Sandbox, SandboxError
from factory.states import RunState, TicketState
from factory.tracker import Tracker
from factory.workbench import Workbench

TICK_SECONDS = 0.2


def new_run_id(clock: Clock) -> str:
    return f"{clock.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def integration_branch(run_id: str) -> str:
    return f"factory/{run_id}"


def ticket_branch(run_id: str, ticket: str) -> str:
    return f"factory/{run_id}--{ticket}"


@dataclass(frozen=True)
class Ports:
    executor: Executor
    sandbox: Sandbox
    workbench: Workbench
    clock: Clock


@dataclass
class RunningAttempt:
    handle: AttemptHandle
    request: AttemptRequest
    head_before: str
    started: datetime


@dataclass
class RunningProbe:
    process: Process
    dir: ProbeDir
    key: dict[str, Any]
    started: datetime


class RefusedHandle:
    """A RefusedHandle stands for an Attempt whose Sandbox did not start the agent."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def poll(self) -> AttemptResult:
        return AttemptResult(output=None, failure="sandbox_start", reason=self._reason)

    def kill(self) -> None:
        pass


@dataclass
class TicketRuntime:
    """A TicketRuntime holds what the Orchestrator keeps in memory about a Ticket between ticks."""

    id: str
    blocked_by: list[str]
    state: TicketState = TicketState.QUEUED
    node: str | None = None
    round: int = 0
    attempt: int = 0
    branch: str = ""
    base: str = ""
    workspace: Path | None = None
    running: RunningAttempt | None = None
    probe: RunningProbe | None = None
    gate_attempt: int = 0

    @property
    def busy(self) -> bool:
        return self.running is not None or self.probe is not None


class Orchestrator:
    def __init__(self, journal: Journal, project: Project, ports: Ports, home: Path, view: RunView) -> None:
        self.journal = journal
        self.project = project
        self.pipeline = project.pipeline
        self.ports = ports
        self.home = home
        self.view = view
        self.git = HostGit(project.repo)
        self.run_id = ""
        self.started = False
        self.tickets: list[TicketRuntime] = []
        self.rounds: dict[tuple[str, str], int] = {}
        self.probe_counts: dict[tuple[str, str], int] = {}

    def start(self, run_id: str, tracker: Tracker) -> None:
        self.run_id = run_id
        self.integration = integration_branch(run_id)
        self.run_dir = RunDir(self.home / "runs" / run_id)
        base_commit = self.git.resolve(self.project.base)
        snapshot = tracker.read_spec()

        self._append(
            "RunStarted",
            {
                "spec_ref": snapshot.ref,
                "project": str(self.project.path),
                "pipeline": str(self.pipeline.path),
                "run_dir": str(self.run_dir.root),
                "integration_branch": self.integration,
            },
        )
        self.started = True
        self._append("SpecSnapshotted", {"snapshot": snapshot.to_json()})
        self._append("SpecRendered", self.run_dir.render(snapshot))
        self.git.create_branch(self.integration, base_commit)
        self._append(
            "IntegrationBranchCreated",
            {"branch": self.integration, "base_ref": self.project.base, "commit": base_commit},
        )
        self.tickets = [TicketRuntime(id=t.id, blocked_by=t.blocked_by) for t in snapshot.tickets]

    def run(self) -> RunState:
        """Ticks until no Ticket can move and returns the Run's final state."""
        self.view.show(self.journal, self.run_id)
        while True:
            moved = self._tick()
            if moved:
                self.view.show(self.journal, self.run_id)
            elif any(t.busy for t in self.tickets):
                self.ports.clock.sleep(TICK_SECONDS)
            else:
                break
        finished = all(t.state in (TicketState.MERGED, TicketState.DONE) for t in self.tickets)
        state = RunState.DONE if finished else RunState.PARTIAL
        self._finish(state)
        return state

    def abort(self, reason: str) -> None:
        """Ends a started Run as `aborted` after an error the Orchestrator cannot carry on from."""
        if self.started:
            self._finish(RunState.ABORTED, reason)

    def _finish(self, state: RunState, reason: str | None = None) -> None:
        self._append("RunFinished", {"state": state, **({"reason": reason} if reason else {})})
        self.view.show(self.journal, self.run_id)

    def _tick(self) -> bool:
        moved = False
        for ticket in self.tickets:
            if ticket.state == TicketState.ACTIVE:
                moved |= self._advance(ticket)
        if not any(t.state == TicketState.ACTIVE for t in self.tickets):
            moved |= self._claim_next()
        return moved

    def _claim_next(self) -> bool:
        """Claims one queued Ticket whose blockers are merged, so Tickets run one after another."""
        merged = {t.id for t in self.tickets if t.state == TicketState.MERGED}
        for ticket in self.tickets:
            if ticket.state == TicketState.QUEUED and set(ticket.blocked_by) <= merged:
                self._claim(ticket)
                return True
        return False

    def _claim(self, ticket: TicketRuntime) -> None:
        ticket.branch = ticket_branch(self.run_id, ticket.id)
        ticket.base = self.git.resolve(self.integration)
        ticket.workspace = self.home / "workspaces" / self.run_id / ticket.id
        self.git.create_branch(ticket.branch, ticket.base)
        self.ports.workbench.create_workspace(self.project.repo, ticket.workspace, ticket.branch)
        self.ports.sandbox.create(self._sandbox_name(ticket), ticket.workspace)
        ticket.state = TicketState.ACTIVE
        self._append(
            "TicketClaimed",
            {"ticket": ticket.id, "branch": ticket.branch, "base": ticket.base, "workspace": str(ticket.workspace)},
        )
        self._enter(ticket, self.pipeline.start)

    def _advance(self, ticket: TicketRuntime) -> bool:
        assert ticket.node is not None
        node = self.pipeline.nodes[ticket.node]
        if isinstance(node, Stage):
            if ticket.running is None:
                self._start_attempt(ticket, node)
                return True
            result = self._poll(ticket.running, node)
            if result is None:
                return False
            outcome = self._finish_attempt(ticket, node, result)

            # A failed Attempt with Attempts left in the Round is replaced on the next tick.
            if outcome is None:
                return True
        elif isinstance(node, Gate):
            if ticket.probe is None:
                self._start_probe(ticket, node)
                return True
            probe = self._poll_probe(ticket, node)
            if probe is None:
                return False
            outcome = self._judge(ticket, node, probe)
        elif isinstance(node, Operation):
            outcome = self._operate(ticket, node)
        else:
            raise AssertionError(f"Config refuses {node.name}, which this engine cannot run yet")
        self._enter(ticket, self.pipeline.route(node, outcome))
        return True

    def _enter(self, ticket: TicketRuntime, target: str) -> None:
        ticket.node = target
        ticket.gate_attempt = 0
        payload: dict[str, Any] = {"ticket": ticket.id, "node": target}
        if isinstance(self.pipeline.nodes.get(target), Stage):
            key = (ticket.id, target)
            self.rounds[key] = self.rounds.get(key, 0) + 1
            ticket.round, ticket.attempt = self.rounds[key], 0
            payload["round"] = ticket.round
        self._append("NodeEntered", payload)
        if target in TERMINAL_NODES and ticket.state == TicketState.ACTIVE:
            ticket.state = TicketState(target)

    def _start_attempt(self, ticket: TicketRuntime, stage: Stage) -> None:
        assert ticket.workspace is not None
        ticket.attempt += 1
        attempt_dir = self.run_dir.attempt(ticket.id, stage.name, ticket.round, ticket.attempt)
        brief = self._brief(ticket, stage, attempt_dir)
        brief_path = write_json_once(attempt_dir / "brief.json", brief)
        request = AttemptRequest(
            stage=stage.name,
            model=stage.model,
            prompt=stage.role.prompt,
            brief=brief_path,
            output_schema=stage.role.output,
            attempt_dir=attempt_dir,
            stream=attempt_dir / "stream.ndjson",
            sandbox=self._sandbox_name(ticket),
        )
        head_before = self.git.head(ticket.workspace, ticket.branch)
        self._append(
            "AttemptStarted",
            {
                **self._attempt_key(ticket),
                "model": stage.model,
                "brief_path": str(brief_path),
                "brief": brief,
                "head_before": head_before,
                "stream": str(request.stream),
            },
        )
        handle: AttemptHandle
        try:
            handle = self.ports.executor.start(request, self.ports.sandbox)
        except SandboxError as error:
            handle = RefusedHandle(str(error))
        ticket.running = RunningAttempt(
            handle=handle, request=request, head_before=head_before, started=self.ports.clock.now()
        )

    def _poll(self, running: RunningAttempt, stage: Stage) -> AttemptResult | None:
        """Polls the Attempt and kills it once it runs past the Stage's timeout."""
        result = running.handle.poll()
        if result is None and (self.ports.clock.now() - running.started).total_seconds() >= stage.timeout:
            running.handle.kill()
            result = AttemptResult(output=None, failure="timeout", reason=f"no result after {stage.timeout:g}s")
        return result

    def _brief(self, ticket: TicketRuntime, stage: Stage, attempt_dir: Path) -> dict[str, Any]:
        """Builds the Brief from pointers to the Stage's declared inputs, the Ticket's base and the output directory."""
        sources = {"spec": self.run_dir.spec, "ticket": self.run_dir.ticket(ticket.id)}
        return {
            "run": self.run_id,
            "ticket": ticket.id,
            "stage": stage.name,
            "round": ticket.round,
            "attempt": ticket.attempt,
            "base": ticket.base,
            "inputs": {item.source: str(sources[item.source]) for item in stage.inputs},
            "output_dir": str(attempt_dir),
        }

    def _finish_attempt(self, ticket: TicketRuntime, stage: Stage, result: AttemptResult) -> str | None:
        """Records how the Attempt ended and returns the Stage outcome, or None when another Attempt follows.

        A failed Attempt leaves its changes in `discarded.patch`, and the Workspace goes back to `head_before`.
        A done Attempt of a `rw` Stage gets its Stage boundary commit.
        A done Attempt of a `ro` Stage has its changes discarded the same way, and the journal records the violation.
        """
        assert ticket.running is not None and ticket.workspace is not None
        running, ticket.running = ticket.running, None
        attempt_dir = running.request.attempt_dir
        errors = self._schema_errors(stage, result.output)

        # The Executor holds the agent to the Role schema, so output that breaks it means the Executor broke.
        failure = result.failure or ("executor_crash" if errors else None)
        if failure is not None:
            record: dict[str, Any] = {
                "outcome": "failed",
                "cause": failure,
                **({"reason": result.reason} if result.reason else {}),
                "errors": errors,
                "head_before": running.head_before,
                **self._discard(ticket, running),
            }
            self._record_attempt(ticket, attempt_dir, record)
            return "failed" if ticket.attempt >= stage.attempts else None

        assert result.output is not None
        output_path = write_json_once(attempt_dir / "output.json", result.output)
        if stage.commits:
            discarded = {}
            head_after = self._commit(ticket, stage, result.output) or running.head_before
        else:
            discarded = self._discard(ticket, running)
            if discarded:
                self._append("WorkspaceViolated", {**self._attempt_key(ticket), **discarded})
            head_after = running.head_before
        record = {
            "outcome": "done",
            "output": str(output_path),
            "head_before": running.head_before,
            "head_after": head_after,
            **discarded,
        }
        self._record_attempt(ticket, attempt_dir, record)
        return "done"

    def _record_attempt(self, ticket: TicketRuntime, attempt_dir: Path, record: dict[str, Any]) -> None:
        write_json_once(attempt_dir / "attempt.json", record)
        self._append("AttemptFinished", {**self._attempt_key(ticket), **record})

    def _commit(self, ticket: TicketRuntime, stage: Stage, output: dict[str, Any]) -> str | None:
        """Makes the Stage boundary commit and returns it, or None when the Attempt changed nothing."""
        assert ticket.workspace is not None
        summary = str(output.get("summary") or "").strip().partition("\n")[0]
        subject = summary or f"{stage.name}: {ticket.id}"
        trailers = {
            "Factory-Run": self.run_id,
            "Factory-Ticket": ticket.id,
            "Factory-Stage": stage.name,
            "Factory-Attempt": str(ticket.attempt),
        }
        commit = self.git.commit_all(ticket.workspace, ticket.branch, subject, trailers)
        if commit is not None:
            self._append("StageBoundaryCommitted", {**self._attempt_key(ticket), "commit": commit, "subject": subject})
        return commit

    def _discard(self, ticket: TicketRuntime, running: RunningAttempt) -> dict[str, Any]:
        """Resets the Workspace to `head_before` and keeps what the Attempt left there in `discarded.patch`."""
        assert ticket.workspace is not None
        patch, files = self.git.discard(ticket.workspace, ticket.branch, running.head_before)
        if not patch:
            return {}
        path = write_once(running.request.attempt_dir / "discarded.patch", patch)
        return {"discarded": str(path), "files": files}

    def _schema_errors(self, stage: Stage, output: dict[str, Any] | None) -> list[str]:
        if output is None:
            return []
        schema = json.loads(stage.role.output.read_text())
        validator = jsonschema.Draft202012Validator(schema)
        return [error.message for error in validator.iter_errors(output)]

    def _start_probe(self, ticket: TicketRuntime, gate: Gate) -> None:
        """Starts the Gate's Probe in the Ticket's Sandbox on the committed state of the Workspace.

        A Probe the Sandbox cannot start is lost, spends no Round and leaves the Gate to start another on the next tick.
        After its last attempt the Gate escalates.
        """
        assert ticket.workspace is not None
        ticket.gate_attempt += 1
        count = (ticket.id, gate.name)
        self.probe_counts[count] = self.probe_counts.get(count, 0) + 1
        probe_dir = self.run_dir.probe(ticket.id, gate.name, self.probe_counts[count])
        key = {
            "ticket": ticket.id,
            "node": gate.name,
            "attempt": ticket.gate_attempt,
            "probe": self.probe_counts[count],
        }
        self.git.reset(ticket.workspace, ticket.branch, ignored=gate.clean)
        self._append(
            "ProbeStarted",
            {
                **key,
                "head": self.git.head(ticket.workspace, ticket.branch),
                "clean": gate.clean,
                "gate_out": str(probe_dir.gate_out),
                "log": str(probe_dir.log),
            },
        )

        # The shell sends the command's stderr to the log too, whichever Sandbox runs it.
        argv = ["sh", "-c", f"exec 2>&1\n{gate.with_['run']}"]
        env = {"GATE_OUT": str(probe_dir.gate_out)}
        try:
            process = self.ports.sandbox.exec(self._sandbox_name(ticket), argv, stdout=probe_dir.log, env=env)
        except SandboxError as error:
            self._append("ProbeLost", {**key, "reason": str(error)})
            if ticket.gate_attempt >= gate.attempts:
                self._enter(ticket, self.pipeline.route(gate, "escalate"))
            return
        ticket.probe = RunningProbe(process=process, dir=probe_dir, key=key, started=self.ports.clock.now())

    def _poll_probe(self, ticket: TicketRuntime, gate: Gate) -> gates.ProbeResult | None:
        """Returns the result of a finished Probe, killing it once it runs past its timeout.

        The Workspace then goes back to its committed state, so nothing the Probe wrote reaches a later commit.
        """
        assert ticket.probe is not None and ticket.workspace is not None
        running = ticket.probe
        code = running.process.poll()
        elapsed = (self.ports.clock.now() - running.started).total_seconds()
        timed_out = code is None and elapsed >= gate.with_["timeout"]
        if code is None and not timed_out:
            return None
        if timed_out:
            running.process.kill()
        self.git.reset(ticket.workspace, ticket.branch, ignored=False)
        return gates.ProbeResult(
            gate_out=running.dir.gate_out,
            log=running.dir.log,
            exit_code=None if timed_out else code,
            timed_out=timed_out,
            duration=elapsed,
        )

    def _judge(self, ticket: TicketRuntime, gate: Gate, probe: gates.ProbeResult) -> str:
        """Writes the verdict on the finished Probe and returns the Gate's outcome."""
        assert ticket.probe is not None
        running, ticket.probe = ticket.probe, None
        verdict = gates.judge(gate.name, gate.kind, gate.with_, probe)
        path = write_json_once(running.dir.verdict, verdict)
        payload = {**running.key, "verdict": str(path), "outcome": verdict["outcome"], "reasons": verdict["reasons"]}
        self._append("GateFinished", payload)
        return str(verdict["outcome"])

    def _operate(self, ticket: TicketRuntime, operation: Operation) -> str:
        tip = self.git.resolve(self.integration)
        head = self.git.resolve(ticket.branch)
        self.git.fast_forward(self.integration, head, tip)
        ticket.state = TicketState.MERGED
        self._append(
            "OperationFinished",
            {
                "ticket": ticket.id,
                "node": operation.name,
                "kind": operation.kind,
                "outcome": "merged",
                "before": tip,
                "after": head,
            },
        )
        return "merged"

    def _sandbox_name(self, ticket: TicketRuntime) -> str:
        return f"{self.run_id}-{ticket.id}"

    def _attempt_key(self, ticket: TicketRuntime) -> dict[str, Any]:
        return {"ticket": ticket.id, "node": ticket.node, "round": ticket.round, "attempt": ticket.attempt}

    def _append(self, type: str, payload: dict[str, Any]) -> None:
        self.journal.append(self.run_id, type, payload)
