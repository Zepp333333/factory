"""The Orchestrator is the deterministic loop that drives one Run and is the only writer of its events."""

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema

from factory import gates
from factory.artifacts import ProbeDir, RunDir, write_json_once, write_once
from factory.clock import Clock
from factory.config import Project
from factory.executor import AttemptHandle, AttemptRequest, AttemptResult, Executor
from factory.git import GitError, HostGit
from factory.journal import Journal
from factory.pipeline import TERMINAL_NODES, Gate, Input, Node, Operation, Stage
from factory.run_view import RunView
from factory.sandbox import Mount, Process, Sandbox, SandboxError, SandboxLost
from factory.states import RunState, TicketState
from factory.tracker import PullRequests, SpecSnapshot, TrackerError
from factory.workbench import Workbench, WorkbenchError

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
    timeout: float
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
    """A TicketRuntime holds what the Orchestrator keeps in memory about a Ticket between ticks.

    `rounds` numbers each Stage's Rounds over the whole Ticket, and `rounds_since_reset` counts them for
    the round limit and model escalation since `reset_rounds` last started the count over.
    `artifacts` lists what the Ticket's Attempts and Probes left, in order, and `cause` is the one whose
    outcome led to the current node.
    `probes` numbers each Gate's Probes over the whole Ticket, and `gate_attempt` counts them since the Ticket
    last entered its current Gate.
    """

    id: str
    blocked_by: list[str]
    state: TicketState = TicketState.QUEUED
    node: str | None = None
    round: int = 0
    attempt: int = 0
    rounds: dict[str, int] = field(default_factory=dict)
    rounds_since_reset: dict[str, int] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    cause: dict[str, Any] | None = None
    branch: str = ""
    base: str = ""
    merged_commit: str = ""
    workspace: Path | None = None
    running: RunningAttempt | None = None
    probe: RunningProbe | None = None
    probes: dict[str, int] = field(default_factory=dict)
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
        self.pull_request = ""
        self.by_hand: list[list[str]] = []

    def start(self, run_id: str, snapshot: SpecSnapshot, pull_requests: PullRequests | None) -> None:
        """Starts the Run on the snapshot the Tracker gave, and lands it in `pull_requests` when there are any."""
        self.run_id = run_id
        self.integration = integration_branch(run_id)
        self.run_dir = RunDir(self.home / "runs" / run_id)
        self.snapshot = snapshot
        self.pull_requests = pull_requests
        base_commit = self.git.resolve(self.project.base)

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
        self._land()
        self._finish(state)
        return state

    def abort(self, reason: str) -> None:
        """Ends a started Run as `aborted` after an error the Orchestrator cannot carry on from."""
        if self.started:
            self._finish(RunState.ABORTED, reason)

    def _land(self) -> None:
        """Pushes the Integration branch and opens its PR into the base ref once at least one Ticket merged.

        The PR's text is an Artifact, so the owner can open the PR by hand from it.
        A step that fails is journaled with its reason and leaves the Run's state alone,
        and `by_hand` then holds the commands that finish the landing.
        """
        if self.pull_requests is None or not any(t.state == TicketState.MERGED for t in self.tickets):
            return
        states = {t.id: t.state for t in self.tickets}
        pull = self.pull_requests.describe(self.snapshot, self.run_id, states)
        body = write_once(self.run_dir.pull_request, pull.body)
        open_command = self.pull_requests.open_command(self.integration, self.project.base, pull.title, body)
        remote = None
        try:
            commit = self.git.resolve(self.integration)
            remote = self._remote(self.pull_requests)
            self.git.push(remote, self.integration)
        except GitError as error:
            self._append("LandingFailed", {"step": "push", "reason": str(error)})
            self.by_hand = [["git", "push", remote or self.pull_requests.url(), self.integration], open_command]
            return
        self._append("IntegrationBranchPushed", {"remote": remote, "branch": self.integration, "commit": commit})
        try:
            self.pull_request = self.pull_requests.open(self.integration, self.project.base, pull.title, body)
        except TrackerError as error:
            self._append("LandingFailed", {"step": "pull_request", "reason": str(error)})
            self.by_hand = [open_command]
            return
        self._append(
            "PullRequestOpened",
            {"url": self.pull_request, "head": self.integration, "base": self.project.base, "body": str(body)},
        )

    def _remote(self, pull_requests: PullRequests) -> str:
        """Returns the first git remote, by name, that points at the repository the PR opens in."""
        for name, url in sorted(self.git.remotes().items()):
            if pull_requests.accepts(url):
                return name
        raise GitError(f"no git remote of {self.project.repo} points at {pull_requests.url()}")

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
        self.ports.sandbox.create(self._sandbox_name(ticket), ticket.workspace, self._mounts(ticket))
        ticket.state = TicketState.ACTIVE
        self._append(
            "TicketClaimed",
            {"ticket": ticket.id, "branch": ticket.branch, "base": ticket.base, "workspace": str(ticket.workspace)},
        )
        self._enter(ticket, self.pipeline.start)

    def _mounts(self, ticket: TicketRuntime) -> list[Mount]:
        """Lists what the Ticket's Sandbox sees besides its Workspace.

        Git in the Sandbox reads the shared `.git` and never writes it, because the host commits (ADR 0009).
        The Ticket's directory is writable, so its Attempts and Probes can leave their exit codes and reports.
        Its earlier Artifacts are read-only files there, but a process in the Sandbox can still delete or replace them,
        and only the Spec and the Ticket's text are mounted read-only.
        """
        assert ticket.workspace is not None
        self.run_dir.agent_home.mkdir(exist_ok=True)
        return [
            Mount(self.git.common),
            Mount(self.run_dir.ticket_dir(ticket.id), writable=True),
            Mount(self.run_dir.spec),
            Mount(self.run_dir.ticket(ticket.id)),
            Mount(self.run_dir.agent_home, writable=True),
            *(Mount(path) for path in self.ports.executor.reads()),
        ]

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
            finished = self._finish_attempt(ticket, node, result)

            # A failed Attempt with Attempts left in the Round is replaced on the next tick.
            if finished is None:
                return True
            outcome, artifact = finished
        elif isinstance(node, Gate):
            if ticket.probe is None:
                self._start_probe(ticket, node)
                return True
            try:
                probe = self._poll_probe(ticket)
            except SandboxLost as error:
                self._lose_probe(ticket, node, ticket.probe.key, str(error))
                return True
            if probe is None:
                return False
            outcome, artifact = self._judge(ticket, node, probe)
        elif isinstance(node, Operation):
            outcome, artifact = self._operate(ticket, node), None
        else:
            raise AssertionError(f"Config refuses {node.name}, which this engine cannot run yet")
        self._leave(ticket, node, outcome, artifact)
        return True

    def _leave(self, ticket: TicketRuntime, node: Node, outcome: str, cause: dict[str, Any] | None) -> None:
        """Takes the route of `outcome` with `cause` for the next node.

        The route first starts the round count of the Stages it resets over.
        """
        ticket.cause = cause
        resets = node.resets.get(outcome, ())
        if resets:
            for stage in resets:
                ticket.rounds_since_reset[stage] = 0
            self._append("RoundsReset", {"ticket": ticket.id, "node": node.name, "stages": list(resets)})
        self._enter(ticket, self.pipeline.route(node, outcome))

    def _enter(self, ticket: TicketRuntime, target: str) -> None:
        """Enters `target`, and starts a Round when it is a Stage.

        A Stage past its round limit is not entered and gives `exhausted`, which keeps the Ticket's cause.
        """
        node = self.pipeline.nodes.get(target)
        if isinstance(node, Stage):
            count = ticket.rounds_since_reset.get(target, 0) + 1
            if node.max_rounds is not None and count > node.max_rounds:
                to = self.pipeline.route(node, "exhausted")
                self._append(
                    "RoundsExhausted",
                    {"ticket": ticket.id, "node": target, "rounds": node.max_rounds, "outcome": "exhausted", "to": to},
                )
                self._leave(ticket, node, "exhausted", ticket.cause)
                return
            ticket.rounds_since_reset[target] = count
            ticket.rounds[target] = ticket.rounds.get(target, 0) + 1
        ticket.node = target
        ticket.gate_attempt = 0
        payload: dict[str, Any] = {"ticket": ticket.id, "node": target}
        if isinstance(node, Stage):
            ticket.round, ticket.attempt = ticket.rounds[target], 0
            payload["round"] = ticket.round
            payload["rounds_since_reset"] = ticket.rounds_since_reset[target]
        self._append("NodeEntered", payload)
        if target in TERMINAL_NODES and ticket.state == TicketState.ACTIVE:
            ticket.state = TicketState(target)
        elif target in TERMINAL_NODES and ticket.state == TicketState.MERGED:
            self._clean_up(ticket)

    def _start_attempt(self, ticket: TicketRuntime, stage: Stage) -> None:
        assert ticket.workspace is not None
        ticket.attempt += 1
        attempt_dir = self.run_dir.attempt(ticket.id, stage.name, ticket.round, ticket.attempt)
        brief = self._brief(ticket, stage, attempt_dir)
        brief_path = write_json_once(attempt_dir / "brief.json", brief)
        model = stage.model_for(ticket.rounds_since_reset[stage.name])
        request = AttemptRequest(
            stage=stage.name,
            model=model,
            profile=stage.profile,
            prompt=stage.role.prompt,
            brief=brief_path,
            output_schema=stage.role.output,
            attempt_dir=attempt_dir,
            stream=attempt_dir / "stream.ndjson",
            sandbox=self._sandbox_name(ticket),
            workspace=ticket.workspace,
            agent_home=self.run_dir.agent_home,
        )
        head_before = self.git.head(ticket.workspace, ticket.branch)
        self._append(
            "AttemptStarted",
            {
                **self._attempt_key(ticket),
                "model": model,
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
        return {
            "run": self.run_id,
            "ticket": ticket.id,
            "stage": stage.name,
            "round": ticket.round,
            "attempt": ticket.attempt,
            "base": ticket.base,
            "inputs": [entry for item in stage.inputs for entry in self._resolve(ticket, item)],
            "output_dir": str(attempt_dir),
        }

    def _resolve(self, ticket: TicketRuntime, item: Input) -> list[dict[str, Any]]:
        """Returns the Brief entries of one input, which is empty only for an optional input with no artifact yet.

        A Stage's entry carries the commit range of its Round, so the agent reads that code with `git diff`.
        """
        if item.source == "spec":
            return [{"source": "spec", "path": str(self.run_dir.spec)}]
        if item.source == "ticket":
            return [{"source": "ticket", "path": str(self.run_dir.ticket(ticket.id))}]
        if item.source == "cause":
            assert ticket.cause is not None, "Config proves that cause has a source on every path"
            return [{"source": "cause", **ticket.cause}]
        found = [a for a in ticket.artifacts if a["kind"] == item.source and a["node"] in item.nodes]
        assert found or item.optional, f"Config proves that {item} has a source on every path"
        picked = found if item.all_rounds else found[-1:]
        return [{"source": item.source, **{k: v for k, v in a.items() if k != "kind"}} for a in picked]

    def _finish_attempt(
        self, ticket: TicketRuntime, stage: Stage, result: AttemptResult
    ) -> tuple[str, dict[str, Any] | None] | None:
        """Records how the Attempt ended and returns the Stage outcome with its artifact, or None when another follows.

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
                **({"resets_at": result.resets_at.isoformat()} if result.resets_at else {}),
                "errors": errors,
                "head_before": running.head_before,
                **self._discard(ticket, running),
            }
            self._record_attempt(ticket, attempt_dir, record)
            return ("failed", None) if ticket.attempt >= stage.attempts else None

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
        artifact = {
            "kind": "stage",
            "node": stage.name,
            "round": ticket.round,
            "attempt": ticket.attempt,
            "path": str(output_path),
            "head_before": running.head_before,
            "head_after": head_after,
        }
        ticket.artifacts.append(artifact)
        return "done", artifact

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

        A Probe the Sandbox cannot start is lost.
        """
        assert ticket.workspace is not None
        ticket.gate_attempt += 1
        number = ticket.probes[gate.name] = ticket.probes.get(gate.name, 0) + 1
        probe_dir = self.run_dir.probe(ticket.id, gate.name, number)
        key = {"ticket": ticket.id, "node": gate.name, "attempt": ticket.gate_attempt, "probe": number}
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
        probe = gates.probe(gate.kind, gate.with_)
        env = {"GATE_OUT": str(probe_dir.gate_out)}
        try:
            process = self.ports.sandbox.exec(self._sandbox_name(ticket), probe.argv, stdout=probe_dir.log, env=env)
        except SandboxError as error:
            self._lose_probe(ticket, gate, key, str(error))
            return
        ticket.probe = RunningProbe(
            process=process, dir=probe_dir, key=key, timeout=probe.timeout, started=self.ports.clock.now()
        )

    def _lose_probe(self, ticket: TicketRuntime, gate: Gate, key: dict[str, Any], reason: str) -> None:
        """Records a Probe that left no result, because its Sandbox did not start it or died under it.

        A lost Probe gets no verdict and spends no Round, and the Gate starts another on the next tick.
        After its last attempt the Gate escalates.
        The Workspace goes back to its committed state, as after a finished Probe.
        """
        assert ticket.workspace is not None
        ticket.probe = None
        self._append("ProbeLost", {**key, "reason": reason})
        self.git.reset(ticket.workspace, ticket.branch, ignored=False)
        if ticket.gate_attempt >= gate.attempts:
            self._leave(ticket, gate, "escalate", None)

    def _poll_probe(self, ticket: TicketRuntime) -> gates.ProbeResult | None:
        """Returns the result of a finished Probe, killing it once it runs past its timeout.

        The Workspace then goes back to its committed state, so nothing the Probe wrote reaches a later commit.
        Raises SandboxLost when the Sandbox died under the Probe.
        """
        assert ticket.probe is not None and ticket.workspace is not None
        running = ticket.probe
        code = running.process.poll()
        elapsed = (self.ports.clock.now() - running.started).total_seconds()
        timed_out = code is None and elapsed >= running.timeout
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

    def _judge(self, ticket: TicketRuntime, gate: Gate, probe: gates.ProbeResult) -> tuple[str, dict[str, Any]]:
        """Writes the verdict on the finished Probe and returns the Gate's outcome with the verdict as its artifact."""
        assert ticket.probe is not None
        running, ticket.probe = ticket.probe, None
        verdict = gates.judge(gate.name, gate.kind, gate.with_, probe)
        path = write_json_once(running.dir.verdict, verdict)
        payload = {**running.key, "verdict": str(path), "outcome": verdict["outcome"], "reasons": verdict["reasons"]}
        self._append("GateFinished", payload)
        artifact = {"kind": "gate", "node": gate.name, "probe": running.key["probe"], "path": str(path)}
        ticket.artifacts.append(artifact)
        return str(verdict["outcome"]), artifact

    def _operate(self, ticket: TicketRuntime, operation: Operation) -> str:
        tip = self.git.resolve(self.integration)
        head = self.git.resolve(ticket.branch)
        self.git.fast_forward(self.integration, head, tip)
        ticket.state, ticket.merged_commit = TicketState.MERGED, head
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

    def _clean_up(self, ticket: TicketRuntime) -> None:
        """Removes the Sandbox, the Workspace and the branch of a merged Ticket, in that order.

        A step that fails is journaled with its reason and keeps what the steps after it would remove,
        and the Ticket stays merged.
        A Sandbox that stays behind stopped for good runs nothing more, so the Workspace and branch still go.
        The branch goes only while it still points at the merged commit, so no later commit is lost with it.
        """
        assert ticket.workspace is not None
        sandbox = self._sandbox_name(ticket)
        try:
            left_behind = self.ports.sandbox.remove(sandbox)
        except (SandboxError, OSError) as error:
            self._cleanup_failed(ticket, "sandbox", error)
            return
        if left_behind is None:
            self._append("SandboxRemoved", {"ticket": ticket.id, "sandbox": sandbox})
        else:
            self._append("SandboxLeftBehind", {"ticket": ticket.id, "sandbox": sandbox, "reason": left_behind})
        try:
            self.git.pin(ticket.workspace, ticket.branch)
            self.ports.workbench.remove_workspace(self.project.repo, ticket.workspace)
        except (GitError, WorkbenchError, OSError) as error:
            self._cleanup_failed(ticket, "workspace", error)
            return
        self._append("WorkspaceRemoved", {"ticket": ticket.id, "workspace": str(ticket.workspace)})
        try:
            self.git.delete_branch(ticket.branch, ticket.merged_commit)
        except GitError as error:
            self._cleanup_failed(ticket, "branch", error)
            return
        self._append(
            "TicketBranchDeleted", {"ticket": ticket.id, "branch": ticket.branch, "commit": ticket.merged_commit}
        )

    def _cleanup_failed(self, ticket: TicketRuntime, step: str, error: Exception) -> None:
        self._append("CleanupFailed", {"ticket": ticket.id, "step": step, "reason": str(error)})

    def _sandbox_name(self, ticket: TicketRuntime) -> str:
        return f"{self.run_id}-{ticket.id}"

    def _attempt_key(self, ticket: TicketRuntime) -> dict[str, Any]:
        return {"ticket": ticket.id, "node": ticket.node, "round": ticket.round, "attempt": ticket.attempt}

    def _append(self, type: str, payload: dict[str, Any]) -> None:
        self.journal.append(self.run_id, type, payload)
