"""Orchestrator: the deterministic loop that drives one Run and is the only writer of its events."""

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
from factory.sandbox import Box, Sandbox
from factory.tracker import Tracker
from factory.workbench import Workbench

TICK_SECONDS = 0.2


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
    """What the Orchestrator holds in memory about a Ticket between ticks."""

    id: str
    blocked_by: list[str]
    state: str = "queued"
    node: str | None = None
    round: int = 0
    attempt: int = 0
    branch: str = ""
    base: str = ""
    workspace: Path | None = None
    box: Box | None = None
    running: RunningAttempt | None = None


def new_run_id(clock: Clock) -> str:
    return f"{clock.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def integration_branch(run_id: str) -> str:
    return f"factory/{run_id}"


class Orchestrator:
    def __init__(self, journal: Journal, project: Project, ports: Ports, home: Path, view: RunView) -> None:
        self.journal = journal
        self.project = project
        self.ports = ports
        self.home = home
        self.view = view
        self.git = HostGit(project.repo)
        self.run_id = ""
        self.integration = ""
        self.rendered: dict[str, Any] = {}
        self.tickets: list[TicketRuntime] = []
        self.rounds: dict[tuple[str, str], int] = {}

    def start(self, run_id: str, tracker: Tracker) -> None:
        self.run_id = run_id
        self.integration = integration_branch(run_id)
        self.run_dir = RunDir(self.home / "runs" / self.run_id)
        base_commit = self.git.resolve(self.project.base)
        snapshot = tracker.read_spec()

        self._append(
            "RunStarted",
            {
                "spec_ref": snapshot.ref,
                "project": str(self.project.path),
                "pipeline": str(self.project.pipeline.path),
                "run_dir": str(self.run_dir.root),
                "integration_branch": self.integration,
            },
        )
        self._append("SpecSnapshotted", {"snapshot": snapshot.to_json()})
        self.rendered = self.run_dir.render(snapshot)
        self._append("SpecRendered", self.rendered)
        self.git.create_branch(self.integration, base_commit)
        self._append(
            "IntegrationBranchCreated",
            {"branch": self.integration, "base_ref": self.project.base, "commit": base_commit},
        )
        self.tickets = [TicketRuntime(id=t.id, blocked_by=t.blocked_by) for t in snapshot.tickets]

    def run(self) -> str:
        """Ticks until no Ticket can move and returns the Run's final state."""
        self.view.show(self.journal, self.run_id)
        while True:
            moved = self._tick()
            if moved:
                self.view.show(self.journal, self.run_id)
            if not moved and not any(t.running for t in self.tickets):
                break
            if not moved:
                self.ports.clock.sleep(TICK_SECONDS)
        state = "done" if all(t.state == "merged" for t in self.tickets) else "partial"
        self._append("RunFinished", {"state": state})
        self.view.show(self.journal, self.run_id)
        return state

    def _tick(self) -> bool:
        moved = False
        for ticket in self.tickets:
            if ticket.state == "active":
                moved |= self._advance(ticket)
        if not any(t.state == "active" for t in self.tickets):
            moved |= self._claim_next()
        return moved

    def _claim_next(self) -> bool:
        merged = {t.id for t in self.tickets if t.state == "merged"}
        for ticket in self.tickets:
            if ticket.state == "queued" and set(ticket.blocked_by) <= merged:
                self._claim(ticket)
                return True
        return False

    def _claim(self, ticket: TicketRuntime) -> None:
        ticket.branch = f"factory/{self.run_id}--{ticket.id}"
        ticket.base = self.git.resolve(self.integration)
        ticket.workspace = self.home / "workspaces" / self.run_id / ticket.id
        self.git.create_branch(ticket.branch, ticket.base)
        self.ports.workbench.create_workspace(self.project.repo, ticket.workspace, ticket.branch)
        ticket.box = self.ports.sandbox.create(f"{self.run_id}-{ticket.id}", ticket.workspace)
        ticket.state = "active"
        self._append(
            "TicketClaimed",
            {
                "ticket": ticket.id,
                "branch": ticket.branch,
                "base": ticket.base,
                "workspace": str(ticket.workspace),
            },
        )
        self._enter(ticket, self.project.pipeline.start)

    def _advance(self, ticket: TicketRuntime) -> bool:
        assert ticket.node is not None
        node = self.project.pipeline.nodes[ticket.node]
        if isinstance(node, Stage):
            if ticket.running is None:
                self._start_attempt(ticket, node)
                return True
            result = ticket.running.handle.poll()
            if result is None:
                return False
            self._route(ticket, node.next, self._finish_attempt(ticket, node, result))
            return True
        self._route(ticket, node.next, self._operate(ticket, node))
        return True

    def _enter(self, ticket: TicketRuntime, target: str) -> None:
        ticket.node = target
        node = self.project.pipeline.nodes.get(target)
        payload: dict[str, Any] = {"ticket": ticket.id, "node": target}
        if isinstance(node, Stage):
            key = (ticket.id, target)
            self.rounds[key] = self.rounds.get(key, 0) + 1
            ticket.round, ticket.attempt = self.rounds[key], 0
            payload["round"] = ticket.round
        self._append("NodeEntered", payload)
        if target in TERMINAL_NODES and ticket.state == "active":
            ticket.state = target

    def _route(self, ticket: TicketRuntime, next: dict[str, str], outcome: str) -> None:
        target = next.get(outcome)
        if target is None:
            ticket.state = "stuck"
            self._append("TicketStuck", {"ticket": ticket.id, "node": ticket.node, "outcome": outcome})
            return
        self._enter(ticket, target)

    def _start_attempt(self, ticket: TicketRuntime, stage: Stage) -> None:
        assert ticket.workspace is not None and ticket.box is not None
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
        handle = self.ports.executor.start(request, ticket.box)
        ticket.running = RunningAttempt(handle=handle, request=request, head_before=head_before)

    def _brief(self, ticket: TicketRuntime, stage: Stage, attempt_dir: Path) -> dict[str, Any]:
        """Builds the Brief: pointers to the Stage's declared inputs, the Ticket's base and the output directory."""
        sources = {"spec": self.rendered["spec"], "ticket": self.rendered["tickets"][ticket.id]}
        return {
            "run": self.run_id,
            "ticket": ticket.id,
            "stage": stage.name,
            "round": ticket.round,
            "attempt": ticket.attempt,
            "base": ticket.base,
            "inputs": {name: sources[name] for name in stage.inputs},
            "output_dir": str(attempt_dir),
        }

    def _finish_attempt(self, ticket: TicketRuntime, stage: Stage, result: AttemptResult) -> str:
        """Records how the Attempt ended, commits its Stage boundary, and returns the Stage outcome."""
        assert ticket.running is not None and ticket.workspace is not None
        running, ticket.running = ticket.running, None
        key = self._attempt_key(ticket)
        failure, errors = result.failure, self._schema_errors(stage, result.output)
        if failure is None and errors:
            failure = "invalid_output"
        if failure is not None:
            self._append("AttemptFinished", {**key, "outcome": "failed", "cause": failure, "errors": errors})
            return "failed"

        assert result.output is not None
        output_path = write_json_once(running.request.attempt_dir / "output.json", result.output)
        subject = result.output.get("summary") or f"{stage.name}: {ticket.id}"
        trailers = {
            "Factory-Run": self.run_id,
            "Factory-Ticket": ticket.id,
            "Factory-Stage": stage.name,
            "Factory-Attempt": str(ticket.attempt),
        }
        commit = self.git.commit_all(ticket.workspace, subject, trailers)
        if commit is not None:
            self._append("StageBoundaryCommitted", {**key, "commit": commit, "subject": subject})
        self._append(
            "AttemptFinished",
            {
                **key,
                "outcome": "done",
                "output": str(output_path),
                "head_before": running.head_before,
                "head_after": commit or running.head_before,
            },
        )
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
        ticket.state = "merged"
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

    def _attempt_key(self, ticket: TicketRuntime) -> dict[str, Any]:
        return {"ticket": ticket.id, "node": ticket.node, "round": ticket.round, "attempt": ticket.attempt}

    def _append(self, type: str, payload: dict[str, Any]) -> None:
        self.journal.append(self.run_id, type, payload)
