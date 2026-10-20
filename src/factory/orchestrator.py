"""The Orchestrator is the deterministic loop that drives one Run and is the only writer of its events."""

import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from factory.artifacts import RunDir, write_json_once
from factory.clock import Clock
from factory.config import TERMINAL_NODES, Operation, Project, Stage
from factory.executor import AttemptHandle, AttemptRequest, AttemptResult, Executor
from factory.git import HostGit
from factory.journal import Journal
from factory.run_view import RunView
from factory.sandbox import Sandbox
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
            elif any(t.running for t in self.tickets):
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
            result = ticket.running.handle.poll()
            if result is None:
                return False
            outcome = self._finish_attempt(ticket, node, result)
        else:
            outcome = self._operate(ticket, node)
        self._enter(ticket, self.pipeline.route(node, outcome))
        return True

    def _enter(self, ticket: TicketRuntime, target: str) -> None:
        ticket.node = target
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
        head_before = self.git.head(ticket.workspace)
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
        handle = self.ports.executor.start(request, self.ports.sandbox)
        ticket.running = RunningAttempt(handle=handle, request=request, head_before=head_before)

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
            "inputs": {name: str(sources[name]) for name in stage.inputs},
            "output_dir": str(attempt_dir),
        }

    def _finish_attempt(self, ticket: TicketRuntime, stage: Stage, result: AttemptResult) -> str:
        """Records how the Attempt ended, commits its Stage boundary, and returns the Stage outcome."""
        assert ticket.running is not None and ticket.workspace is not None
        running, ticket.running = ticket.running, None
        attempt_dir = running.request.attempt_dir
        errors = self._schema_errors(stage, result.output)
        failure = result.failure or ("invalid_output" if errors else None)
        if failure is not None:
            record = {"outcome": "failed", "cause": failure, "errors": errors, "head_before": running.head_before}
            write_json_once(attempt_dir / "attempt.json", record)
            self._append("AttemptFinished", {**self._attempt_key(ticket), **record})
            return "failed"

        assert result.output is not None
        output_path = write_json_once(attempt_dir / "output.json", result.output)
        summary = str(result.output.get("summary") or "").strip().partition("\n")[0]
        subject = summary or f"{stage.name}: {ticket.id}"
        trailers = {
            "Factory-Run": self.run_id,
            "Factory-Ticket": ticket.id,
            "Factory-Stage": stage.name,
            "Factory-Attempt": str(ticket.attempt),
        }
        commit = self.git.commit_all(ticket.workspace, subject, trailers)
        if commit is not None:
            self._append("StageBoundaryCommitted", {**self._attempt_key(ticket), "commit": commit, "subject": subject})
        record = {
            "outcome": "done",
            "output": str(output_path),
            "head_before": running.head_before,
            "head_after": commit or running.head_before,
        }
        write_json_once(attempt_dir / "attempt.json", record)
        self._append("AttemptFinished", {**self._attempt_key(ticket), **record})
        return "done"

    def _schema_errors(self, stage: Stage, output: dict[str, Any] | None) -> list[str]:
        if output is None:
            return []
        schema = json.loads(stage.role.output.read_text())
        validator = jsonschema.Draft202012Validator(schema)
        return [error.message for error in validator.iter_errors(output)]

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
