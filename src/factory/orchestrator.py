"""The Orchestrator is the deterministic loop that drives one Run and is the only writer of its events."""

import json
import re
import secrets
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema

from factory import deciders, gates
from factory.artifacts import ProbeDir, RunDir, write_json_once, write_once
from factory.clock import Clock
from factory.config import Project
from factory.executor import SANDBOX_FAILURES, AttemptHandle, AttemptRequest, AttemptResult, Executor
from factory.git import GitError, HostGit, MergeConflict
from factory.journal import DESCRIBER_SLOT, CommandRow, Journal
from factory.merge_window import MergeWindow
from factory.pipeline import TERMINAL_NODES, Decision, Describer, Gate, Input, Node, Operation, Role, Stage
from factory.question import CardOutcome, Link, render_card
from factory.run_view import RunView
from factory.sandbox import Mount, Process, Sandbox, SandboxError, SandboxLost
from factory.states import RunState, TicketState
from factory.tracker import PullRequest, PullRequestFolder, PullRequests, SpecSnapshot, Tracker, TrackerError
from factory.workbench import TERMINAL_LOG, AttemptTerminal, Workbench, WorkbenchError

TICK_SECONDS = 0.2

# A rate limit that gives no reset time still in the future pauses new Attempts for this many seconds.
RATE_LIMIT_PAUSE = 600

# The PR body the Describer writes holds these sections, and `Merge Danger` holds both of these entries.
PR_SECTIONS = ("Summary", "Evidence", "Merge Danger")
PR_DANGER = ("Door", "Blast Radius")


def new_run_id(clock: Clock) -> str:
    return f"{clock.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def integration_branch(run_id: str) -> str:
    return f"factory/{run_id}"


def abandoned_ref(run_id: str, ticket: str) -> str:
    return f"refs/factory/{run_id}/abandoned/{ticket}"


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
    ignored_before: frozenset[str]
    terminal: AttemptTerminal | None = None


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


@dataclass(frozen=True)
class Conflict:
    """A Conflict is the merge of `tip` that `sync` left for `resolve` to edit and the host to commit."""

    tip: str
    files: list[str]


class Rejected(Exception):
    """A Rejected is the reason the Orchestrator turns a Command down."""


@dataclass(frozen=True)
class Landing:
    """A Landing is one `merge` of a Ticket, which moved the Integration branch from `before` to `after`."""

    ticket: str
    before: str
    after: str


@dataclass
class TicketRuntime:
    """A TicketRuntime holds what the Orchestrator keeps in memory about a Ticket between ticks.

    `rounds` numbers each Stage's Rounds over the whole Ticket, and `rounds_since_reset` counts them for
    the round limit and model escalation since `reset_rounds` last started the count over.
    `artifacts` lists what the Ticket's Attempts and Probes left, in order, and `cause` is the one whose
    outcome led to the current node.
    `probes` numbers each Gate's Probes over the whole Ticket, and `gate_attempt` counts them since the Ticket
    last entered its current Gate.
    `probe_results` keeps each Gate's latest finished Probe with its number, for the Gates that read it with `from`.
    `spent` counts the Attempts of the current Round that count toward the Stage's `attempts`.
    `sandbox_losses` counts the Attempts and Probes of the whole Ticket that its Sandbox lost.
    `conflict` is the merge `sync` left in the Workspace until the host commits it after `resolve`.
    `synced` is the Integration branch tip the Ticket branch last took in, `base` at claim and then each `sync`'s.
    The Brief and `min_changed` diff from it, so they see only the work of this Ticket.
    `waiting` is the node and Round of the human Decision the Ticket waits at, and `answered` tells that a human
    has decided a Decision of the Ticket.
    """

    id: str
    blocked_by: list[str]
    state: TicketState = TicketState.QUEUED
    node: str | None = None
    round: int = 0
    attempt: int = 0
    spent: int = 0
    sandbox_losses: int = 0
    rounds: dict[str, int] = field(default_factory=dict)
    rounds_since_reset: dict[str, int] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    cause: dict[str, Any] | None = None
    branch: str = ""
    base: str = ""
    merged_commit: str = ""
    kept: bool = False
    workspace: Path | None = None
    running: RunningAttempt | None = None
    probe: RunningProbe | None = None
    probes: dict[str, int] = field(default_factory=dict)
    probe_results: dict[str, tuple[int, gates.ProbeResult]] = field(default_factory=dict)
    gate_attempt: int = 0
    conflict: Conflict | None = None
    synced: str = ""
    waiting: tuple[str, int] | None = None
    answered: bool = False
    marked: str = ""

    @property
    def busy(self) -> bool:
        return self.running is not None or self.probe is not None


class Orchestrator:
    def __init__(self, journal: Journal, project: Project, ports: Ports, home: Path, view: RunView, slots: int) -> None:
        self.journal = journal
        self.project = project
        self.pipeline = project.pipeline
        self.ports = ports
        self.home = home
        self.view = view
        self.slots = slots
        self.git = HostGit(project.repo)
        self.run_id = ""
        self.started = False
        self.tickets: list[TicketRuntime] = []
        self.pull_request = ""
        self.by_hand: list[list[str]] = []
        self.paused_until: datetime | None = None
        self.starved = False
        self.window = MergeWindow(self._append)
        self.landings: list[Landing] = []

    def start(self, run_id: str, snapshot: SpecSnapshot, tracker: Tracker) -> None:
        """Starts the Run on the snapshot the Tracker gave, and lands it in the Tracker's PRs when it has any."""
        self.run_id = run_id
        self.integration = integration_branch(run_id)
        self.run_dir = RunDir(self.home / "runs" / run_id)
        self.snapshot = snapshot
        self.tracker = tracker
        base_commit = self.base_commit = self.git.resolve(self.project.base)

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
            elif self.starved or any(t.busy or t.waiting for t in self.tickets):
                self.ports.clock.sleep(TICK_SECONDS)
            elif self.paused_until is not None:
                self.ports.clock.sleep((self.paused_until - self.ports.clock.now()).total_seconds())
            else:
                break
        finished = all(t.state in (TicketState.MERGED, TicketState.DONE) for t in self.tickets)
        state = RunState.DONE if finished else RunState.PARTIAL
        self._land()
        self._finish(state)
        return state

    def abort(self, reason: str) -> None:
        """Ends a started Run as `aborted` after an error or a stop the Orchestrator cannot carry on from.

        It kills the agents and Probes still running, so nothing works on for a Run that has ended.
        """
        if not self.started:
            return
        for ticket in self.tickets:
            if ticket.running is not None:
                ticket.running.handle.kill()
                self._close_terminal(ticket.running.terminal)
            if ticket.probe is not None:
                ticket.probe.process.kill()
        self._finish(RunState.ABORTED, reason)

    def _land(self) -> None:
        """Writes the PR text of a Run that merged a Ticket, then pushes the Integration branch and opens its PR.

        The text is an Artifact in any case, and a Tracker without PRs records the PR in its folder.
        A step that fails is journaled with its reason and leaves the Run's state alone,
        and `by_hand` then holds the commands that finish the landing.
        """
        if not any(t.state == TicketState.MERGED for t in self.tickets):
            return
        states = {t.id: t.state for t in self.tickets}
        kept = {t.id: abandoned_ref(self.run_id, t.id) for t in self.tickets if t.kept}
        pull = self._describe(states, kept)
        body = write_once(self.run_dir.pull_request, pull.body)
        pull_requests = self.tracker.pull_requests
        if isinstance(pull_requests, PullRequestFolder):
            self.pull_request = str(pull_requests.record(self.integration, self.project.base, pull.title, body))
            self._append(
                "PullRequestRecorded", {"path": self.pull_request, "head": self.integration, "base": self.project.base}
            )
            return
        open_command = pull_requests.open_command(self.integration, self.project.base, pull.title, body)
        remote = None
        try:
            commit = self.git.resolve(self.integration)
            remote = self._remote(pull_requests)
            self.git.push(remote, self.integration)
        except GitError as error:
            self._append("LandingFailed", {"step": "push", "reason": str(error)})
            self.by_hand = [["git", "push", remote or pull_requests.url(), self.integration], open_command]
            return
        self._append("IntegrationBranchPushed", {"remote": remote, "branch": self.integration, "commit": commit})
        try:
            self.pull_request = pull_requests.open(self.integration, self.project.base, pull.title, body)
        except TrackerError as error:
            self._append("LandingFailed", {"step": "pull_request", "reason": str(error)})
            self.by_hand = [open_command]
            return
        self._append(
            "PullRequestOpened",
            {"url": self.pull_request, "head": self.integration, "base": self.project.base, "body": str(body)},
        )

    def _describe(self, states: dict[str, TicketState], kept: dict[str, str]) -> PullRequest:
        """Returns the PR text, which the Describer writes when the Pipeline has one.

        The engine appends its own footer to the Describer's body, so closing the Tickets does not depend on the agent.
        The Tracker defuses the closing keywords in that body, so only the footer closes issues.
        A Describer that fails every Attempt is journaled with the reason of each, and the Tracker's own text stands in.
        """
        fallback = self.tracker.describe(self.snapshot, self.run_id, states, kept)
        describer = self.pipeline.describer
        if describer is None:
            return fallback
        for attempt in range(1, describer.attempts + 1):
            result = self._describe_attempt(describer, attempt)
            problems = self._pull_request_problems(describer.role, result)
            if not problems:
                assert result.output is not None
                footer = self.tracker.footer(self.snapshot, states, kept)
                body = f"{self.tracker.defuse(str(result.output['body'])).strip()}\n\n{footer}"
                self._append("PullRequestDescribed", {"attempt": attempt, "title": result.output["title"]})
                return PullRequest(title=str(result.output["title"]).strip(), body=body)
            cause = result.failure or "invalid_output"
            self._append("DescriberFailed", {"attempt": attempt, "cause": cause, "reason": problems})
        return fallback

    def _pull_request_problems(self, role: Role, result: AttemptResult) -> str:
        """Tells why the Describer's result is no PR text, or returns an empty text when it is one."""
        if result.failure is not None:
            return result.reason or result.failure
        errors = self._schema_errors(role, result.output)
        if errors or result.output is None:
            return "; ".join(errors)
        body = str(result.output["body"])
        missing = [name for name in PR_SECTIONS if not re.search(rf"^#+\s+{name}\s*$", body, re.MULTILINE)]
        missing += [name for name in PR_DANGER if name not in body]
        return f"the body lacks {', '.join(missing)}" if missing else ""

    def _describe_attempt(self, describer: Describer, attempt: int) -> AttemptResult:
        """Runs one Attempt of the Describer in a Workspace on a branch at the Integration branch's tip.

        Its changes are discarded, and the Workspace, the Sandbox and the branch are removed after it.
        A Sandbox or git step that fails ends the Attempt as `sandbox_start`.
        """
        name = f"{self.run_id}-describe"
        branch = f"factory/{self.run_id}--describe"
        workspace = self.home / "workspaces" / self.run_id / "describe"
        tip = self.git.resolve(self.integration)
        attempt_dir = self.run_dir.describer / f"attempt-{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.git.create_branch(branch, tip)
            self.ports.workbench.create_workspace(self.project.repo, workspace, branch)
            self.ports.sandbox.create(name, workspace, self._describer_mounts())
            brief = self._describer_brief(attempt, attempt_dir)
            brief_path = write_json_once(attempt_dir / "brief.json", brief)
            model = describer.model
            request = AttemptRequest(
                stage="describe",
                model=model,
                profile=describer.profile,
                prompt=describer.role.prompt,
                brief=brief_path,
                output_schema=describer.role.output,
                attempt_dir=attempt_dir,
                stream=attempt_dir / "stream.ndjson",
                sandbox=name,
                workspace=workspace,
                agent_home=self.run_dir.agent_home,
            )
            self._wait_for_slot(
                DESCRIBER_SLOT,
                "DescriberStarted",
                {"attempt": attempt, "model": model, "brief_path": str(brief_path), "brief": brief, "head": tip},
            )
            result = self._run_describer(describer, request)
        except (SandboxError, WorkbenchError, GitError, OSError) as error:
            result = AttemptResult(output=None, failure="sandbox_start", reason=str(error))
        self._remove_describer(name, workspace, branch, tip, attempt_dir)
        return result

    def _wait_for_slot(self, holder: str, type: str, payload: dict[str, Any]) -> None:
        """Waits until a slot of the machine is free, then takes it and journals the event in one transaction."""
        while True:
            with self.journal.transaction():
                if self.journal.take_slot(self.run_id, holder, self.slots):
                    self._append(type, payload)
                    return
            self.ports.clock.sleep(TICK_SECONDS)

    def _run_describer(self, describer: Describer, request: AttemptRequest) -> AttemptResult:
        """Waits for the Describer's agent, which is killed once it runs past the timeout."""
        handle = self.ports.executor.start(request, self.ports.sandbox)
        terminal = self._open_terminal(request)
        started = self.ports.clock.now()
        try:
            while (result := handle.poll()) is None:
                if (self.ports.clock.now() - started).total_seconds() >= describer.timeout:
                    handle.kill()
                    return AttemptResult(
                        output=None, failure="timeout", reason=f"no result after {describer.timeout:g}s"
                    )
                self.ports.clock.sleep(TICK_SECONDS)
        finally:
            self._close_terminal(terminal)
        return result

    def _open_terminal(self, request: AttemptRequest) -> AttemptTerminal | None:
        """Opens the owner's terminal on the Attempt, and goes on without one when the Workbench cannot."""
        try:
            return self.ports.workbench.open_attempt(
                request.stream, request.attempt_dir / TERMINAL_LOG, request.workspace, request.stage
            )
        except (WorkbenchError, OSError):
            return None

    def _close_terminal(self, terminal: AttemptTerminal | None) -> None:
        if terminal is not None:
            terminal.close()

    def _remove_describer(self, name: str, workspace: Path, branch: str, tip: str, attempt_dir: Path) -> None:
        """Removes what the Describer's Attempt used, and keeps what its agent changed in `discarded.patch`.

        A step that fails is journaled with its reason and leaves the steps after it undone.
        """
        try:
            self.ports.sandbox.remove(name)
            if workspace.exists():
                patch, _ = self.git.discard(workspace, branch, tip)
                if patch:
                    write_once(attempt_dir / "discarded.patch", patch)
                self.git.pin(workspace, branch)
                self.ports.workbench.remove_workspace(self.project.repo, workspace)
            self.git.delete_branch(branch, tip)
        except (SandboxError, WorkbenchError, GitError, OSError) as error:
            self._append("DescriberCleanupFailed", {"reason": str(error)})

    def _describer_mounts(self) -> list[Mount]:
        """Lists what the Describer's Sandbox sees besides its Workspace: the Run's Artifacts, read-only."""
        self.run_dir.agent_home.mkdir(exist_ok=True)
        self.run_dir.describer.mkdir(exist_ok=True)
        return [
            Mount(self.git.common),
            Mount(self.run_dir.spec),
            Mount(self.run_dir.root / "tickets"),
            Mount(self.run_dir.describer, writable=True),
            Mount(self.run_dir.agent_home, writable=True),
            *(Mount(path) for path in self.ports.executor.reads()),
        ]

    def _describer_brief(self, attempt: int, attempt_dir: Path) -> dict[str, Any]:
        """Builds the Describer's Brief from the Spec, the merged Tickets with their Gate verdicts and Stage outputs,
        the base ref and the Integration branch.
        """
        inputs: list[dict[str, Any]] = [{"source": "spec", "path": str(self.run_dir.spec)}]
        for ticket in self.tickets:
            if ticket.state != TicketState.MERGED:
                continue
            inputs.append({"source": "ticket", "ticket": ticket.id, "path": str(self.run_dir.ticket(ticket.id))})
            for artifact in ticket.artifacts:
                entry = {k: v for k, v in artifact.items() if k != "kind"}
                inputs.append({"source": artifact["kind"], "ticket": ticket.id, **entry})
        return {
            "run": self.run_id,
            "stage": "describe",
            "attempt": attempt,
            "base": self.base_commit,
            "base_ref": self.project.base,
            "integration_branch": self.integration,
            "inputs": inputs,
            "output_dir": str(attempt_dir),
        }

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
        moved = self._resume()
        self.view.prompt(self.journal, self.run_id)
        moved |= self._apply_commands()
        self.starved = False
        for ticket in self.tickets:
            if ticket.state == TicketState.ACTIVE:
                self._mark_stage(ticket)
                moved |= self._advance(ticket)
        return moved | self._claim_next()

    def _mark_stage(self, ticket: TicketRuntime) -> None:
        """Shows the Ticket's node and Round on the card of its Workspace once they change.

        A Workbench that cannot show it is not a reason to stop the Ticket.
        """
        node = self.pipeline.nodes.get(ticket.node or "")
        if ticket.workspace is None or node is None or not ticket.workspace.exists():
            return
        text = f"{ticket.node}, Round {ticket.round}" if isinstance(node, Stage | Decision) else str(ticket.node)
        if text == ticket.marked:
            return
        try:
            self.ports.workbench.mark_stage(ticket.workspace, text)
        except (WorkbenchError, OSError):
            return
        ticket.marked = text

    def _pause(self, ticket: TicketRuntime, result: AttemptResult) -> None:
        """Holds back new Attempts of every Ticket until the subscription's limit resets.

        A reset time that is missing or already past gives the fixed pause.
        A pause that already runs longer stays as it is.
        """
        now = self.ports.clock.now()
        until = result.resets_at
        if until is None or until <= now:
            until = now + timedelta(seconds=RATE_LIMIT_PAUSE)
        if self.paused_until is not None and self.paused_until >= until:
            return
        self.paused_until = until
        payload = {"resets_at": until.isoformat(), **self._attempt_key(ticket)}
        self._append("RunPaused", {**payload, **({"reason": result.reason} if result.reason else {})})

    def _resume(self) -> bool:
        """Ends the pause once its reset time comes, and returns whether it did."""
        if self.paused_until is None or self.ports.clock.now() < self.paused_until:
            return False
        self._append("RunResumed", {"resets_at": self.paused_until.isoformat()})
        self.paused_until = None
        return True

    def _claim_next(self) -> bool:
        """Claims the Frontier's queued Tickets in order, up to `max_parallel` active ones, and returns whether it did.

        A Ticket is on the Frontier once all its blockers are merged.
        Claiming stops at the first Ticket that finds no free slot, and waits out a pause like any new Attempt.
        """
        merged = {t.id for t in self.tickets if t.state == TicketState.MERGED}
        claimed = False
        for ticket in self.tickets:
            active = sum(1 for t in self.tickets if t.state == TicketState.ACTIVE)
            if active >= self.project.max_parallel or self.paused_until is not None:
                break
            if ticket.state == TicketState.QUEUED and set(ticket.blocked_by) <= merged:
                if not self._claim(ticket):
                    break
                claimed = True
        return claimed

    def _claim(self, ticket: TicketRuntime) -> bool:
        """Claims the Ticket and starts its first Attempt, and returns False when no slot is free.

        The slot, the claim and the record of the Attempt are one transaction of the Journal, so no other Run
        can take the slot in between.
        The Workspace starts from the Integration branch's tip as it is now, and the Sandbox is created after the claim.
        A Pipeline that starts at a Gate or an Operation needs no slot to claim a Ticket.
        """
        start = self.pipeline.nodes[self.pipeline.start]
        stage = start if isinstance(start, Stage) else None
        base = self.git.resolve(self.integration)
        with self.journal.transaction():
            if stage is not None and not self._take_slot(ticket):
                return False
            ticket.branch = ticket_branch(self.run_id, ticket.id)
            ticket.base = ticket.synced = base
            ticket.workspace = self.home / "workspaces" / self.run_id / ticket.id
            ticket.state = TicketState.ACTIVE
            self._append(
                "TicketClaimed",
                {"ticket": ticket.id, "branch": ticket.branch, "base": ticket.base, "workspace": str(ticket.workspace)},
            )
            self._enter(ticket, self.pipeline.start)
            request = self._begin_attempt(ticket, stage, base) if stage is not None else None
        self.git.create_branch(ticket.branch, ticket.base)
        self.ports.workbench.create_workspace(self.project.repo, ticket.workspace, ticket.branch)
        self.ports.sandbox.create(self._sandbox_name(ticket), ticket.workspace, self._mounts(ticket))
        if request is not None:
            self._launch(ticket, request, base)
        return True

    def _take_slot(self, ticket: TicketRuntime) -> bool:
        """Takes a slot of the machine's cap for the Ticket's next Attempt, and notes when none is free."""
        if self.journal.take_slot(self.run_id, ticket.id, self.slots):
            return True
        self.starved = True
        return False

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
                # Only Attempts wait out a pause, because Probes and Operations do not use the subscription.
                if self.paused_until is not None:
                    return False
                return self._start_attempt(ticket, node)
            result = self._poll(ticket.running, node)
            if result is None:
                return False
            finished = self._finish_attempt(ticket, node, result)

            # A failed Attempt with Attempts left in the Round is replaced on the next tick, or after the pause.
            if finished is None:
                return True
            outcome, artifact = finished
            if artifact is not None:
                ticket.artifacts.append(artifact)
        elif isinstance(node, Decision):
            if node.decider.kind == "human":
                return self._wait_for_human(ticket, node)
            if node.decider.kind == "function":
                ruling = self._call_function(ticket, node)
            else:
                stage = self._decider_stage(node)
                if ticket.running is None:
                    return self.paused_until is None and self._start_attempt(ticket, stage)
                result = self._poll(ticket.running, stage)
                if result is None:
                    return False
                finished = self._finish_attempt(ticket, stage, result, node.outcomes)
                if finished is None:
                    return True
                ruling = self._model_ruling(ticket, finished)
            outcome, artifact = self._record_decision(ticket, node, ruling)
        elif isinstance(node, Gate) and node.from_ is not None:
            outcome, artifact = self._judge_borrowed(ticket, node, node.from_)
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
            if not self.window.hold(ticket.id, node.name):
                return False
            outcome, artifact = self._operate(ticket, node)
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
        """Enters `target`, and starts a Round when it is a Stage or a Decision.

        A Stage past its round limit is not entered and gives `exhausted`, which keeps the Ticket's cause.
        """
        node = self.pipeline.nodes.get(target)
        if node is None or not node.HOLDS_WINDOW:
            self.window.release(ticket.id, ticket.node)
        if isinstance(node, Stage | Decision):
            count = ticket.rounds_since_reset.get(target, 0) + 1
            if isinstance(node, Stage) and node.max_rounds is not None and count > node.max_rounds:
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
        if isinstance(node, Stage | Decision):
            ticket.round, ticket.attempt, ticket.spent = ticket.rounds[target], 0, 0
            payload["round"] = ticket.round
            payload["rounds_since_reset"] = ticket.rounds_since_reset[target]
        self._append("NodeEntered", payload)
        if target in TERMINAL_NODES and ticket.state == TicketState.ACTIVE:
            ticket.state = TicketState(target)
        elif target in TERMINAL_NODES and ticket.state == TicketState.MERGED:
            self._clean_up(ticket, ticket.merged_commit)
        if target == "abandoned" and ticket.state == TicketState.ABANDONED:
            self._abandon(ticket)

    def _start_attempt(self, ticket: TicketRuntime, stage: Stage) -> bool:
        """Starts the Ticket's next Attempt of the Stage, or returns False when no slot is free."""
        assert ticket.workspace is not None
        head_before = self.git.head(ticket.workspace, ticket.branch)
        with self.journal.transaction():
            if not self._take_slot(ticket):
                return False
            request = self._begin_attempt(ticket, stage, head_before)
        self._launch(ticket, request, head_before)
        return True

    def _begin_attempt(self, ticket: TicketRuntime, stage: Stage, head_before: str) -> AttemptRequest:
        """Writes the Attempt's Brief and journals its start, and returns the request the Executor will run."""
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
        return request

    def _launch(self, ticket: TicketRuntime, request: AttemptRequest, head_before: str) -> None:
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
        ticket.running = RunningAttempt(
            terminal=terminal,
            handle=handle,
            request=request,
            head_before=head_before,
            started=self.ports.clock.now(),
            ignored_before=ignored_before,
        )

    def _poll(self, running: RunningAttempt, stage: Stage) -> AttemptResult | None:
        """Polls the Attempt and kills it once it runs past the Stage's timeout."""
        result = running.handle.poll()
        if result is None and (self.ports.clock.now() - running.started).total_seconds() >= stage.timeout:
            running.handle.kill()
            result = AttemptResult(output=None, failure="timeout", reason=f"no result after {stage.timeout:g}s")
        return result

    def _brief(self, ticket: TicketRuntime, stage: Stage, attempt_dir: Path) -> dict[str, Any]:
        """Builds the Brief from the Stage's declared inputs, the Ticket's base, the Release channel and the output."""
        node = self.pipeline.nodes[stage.name]
        return {
            "run": self.run_id,
            "ticket": ticket.id,
            "stage": stage.name,
            "round": ticket.round,
            "attempt": ticket.attempt,
            "base": ticket.synced,
            **({"release": self.project.release} if self.project.release else {}),
            "inputs": [entry for item in stage.inputs for entry in self._resolve(ticket, item)],
            **({"outcomes": list(node.outcomes)} if isinstance(node, Decision) else {}),
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
        self, ticket: TicketRuntime, stage: Stage, result: AttemptResult, outcomes: tuple[str, ...] | None = None
    ) -> tuple[str, dict[str, Any] | None] | None:
        """Records how the Attempt ended and returns the Stage outcome with its artifact, or None when another follows.

        A failed Attempt leaves its changes in `discarded.patch`, and the Workspace goes back to `head_before`.
        A `rate_limited` Attempt spends no Attempt of the Stage and pauses the Run.
        An Attempt the Sandbox lost spends none either, and counts toward `sandbox_retries` of the Ticket,
        so the Stage ends `failed` once the Ticket has lost its Sandbox more often than that.
        A done Attempt of a `rw` Stage gets its Stage boundary commit.
        A done Attempt of a `ro` Stage has its changes discarded the same way, and the journal records the violation.
        With `outcomes` set, the Attempt is a model Decider's, and an output that names none of them fails it.
        """
        assert ticket.running is not None and ticket.workspace is not None
        running, ticket.running = ticket.running, None
        self._close_terminal(running.terminal)
        attempt_dir = running.request.attempt_dir
        errors = self._schema_errors(stage.role, result.output)
        if not errors and outcomes is not None and result.output is not None:
            errors = self._outcome_errors(result.output, outcomes)

        # The Executor holds the agent to the Role schema, so output that breaks it means the Executor broke.
        failure = result.failure or ("executor_crash" if errors else None)
        if failure is None and stage.commits and ticket.conflict is not None:
            left = self.git.markers(ticket.workspace, ticket.conflict.files)
            errors = [f"{name} still holds conflict markers" for name in left]
            failure = "conflict_markers" if errors else None
        strays = self._remove_stray_ignored(ticket, running)
        removed = {"removed_ignored": strays} if strays else {}
        if failure is None and stage.commits and strays:
            errors = [f"{name} is ignored by .gitignore, so the Stage boundary commit leaves it out" for name in strays]
            failure = "ignored_files"
        if failure is not None:
            record: dict[str, Any] = {
                "outcome": "failed",
                "cause": failure,
                **({"reason": result.reason} if result.reason else {}),
                **({"resets_at": result.resets_at.isoformat()} if result.resets_at else {}),
                "errors": errors,
                "head_before": running.head_before,
                **self._discard(ticket, running),
                **removed,
            }
            self._record_attempt(ticket, attempt_dir, record)
            if failure == "rate_limited":
                self._pause(ticket, result)
            elif failure in SANDBOX_FAILURES:
                ticket.sandbox_losses += 1
                return ("failed", None) if self._retries_exhausted(ticket) else None
            else:
                ticket.spent += 1
            return ("failed", None) if ticket.spent >= stage.attempts else None

        assert result.output is not None
        output_path = write_json_once(attempt_dir / "output.json", result.output)
        if stage.commits:
            discarded = {}
            head_after = self._commit(ticket, stage, result.output) or running.head_before
        else:
            discarded = {**self._discard(ticket, running), **removed}
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
        return "done", artifact

    def _remove_stray_ignored(self, ticket: TicketRuntime, running: RunningAttempt) -> list[str]:
        """Removes the ignored files the Attempt created outside the project's `caches`, and returns their names.

        Gates would see them in the Workspace, but `merge` would land the tree without them.
        """
        assert ticket.workspace is not None
        new = self.git.ignored(ticket.workspace, ticket.branch) - running.ignored_before
        strays = sorted(name for name in new if self.project.caches.isdisjoint(Path(name).parts))
        self.git.remove_untracked(ticket.workspace, ticket.branch, strays)
        return strays

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
        merging = ticket.conflict.tip if ticket.conflict else None
        commit = self.git.commit_all(ticket.workspace, ticket.branch, subject, trailers, merging)
        if ticket.conflict is not None:
            ticket.synced = ticket.conflict.tip
        ticket.conflict = None
        if commit is not None:
            self._append("StageBoundaryCommitted", {**self._attempt_key(ticket), "commit": commit, "subject": subject})
        return commit

    def _discard(self, ticket: TicketRuntime, running: RunningAttempt) -> dict[str, Any]:
        """Resets the Workspace to `head_before` and keeps what the Attempt left there in `discarded.patch`."""
        assert ticket.workspace is not None
        patch, files = self.git.discard(ticket.workspace, ticket.branch, running.head_before)
        if ticket.conflict is not None:
            # The merge is what `resolve` works on, so the next Attempt starts from the same conflict.
            self.git.begin_merge(ticket.workspace, ticket.branch, ticket.conflict.tip)
        if not patch:
            return {}
        path = write_once(running.request.attempt_dir / "discarded.patch", patch)
        return {"discarded": str(path), "files": files}

    def _schema_errors(self, role: Role, output: dict[str, Any] | None) -> list[str]:
        if output is None:
            return []
        schema = json.loads(role.output.read_text())
        validator = jsonschema.Draft202012Validator(schema)
        return [error.message for error in validator.iter_errors(output)]

    def _outcome_errors(self, output: dict[str, Any], outcomes: tuple[str, ...]) -> list[str]:
        """Tells why a model Decider's output is no outcome from the closed list with reasons."""
        errors = []
        if output.get("outcome") not in outcomes:
            errors.append(f"outcome {output.get('outcome')!r} is not one of {', '.join(outcomes)}")
        reasons = output.get("reasons", [])
        if not isinstance(reasons, list) or not all(isinstance(reason, str) for reason in reasons):
            errors.append("reasons must be a list of strings")
        return errors

    def _decider_stage(self, decision: Decision) -> Stage:
        """Returns the read-only Stage a model Decider's Attempts run as."""
        decider = decision.decider
        assert decider.role is not None, "Config proves that a model Decider has a Role"
        return Stage(
            name=decision.name,
            role=decider.role,
            model=decider.model,
            inputs=decision.inputs,
            next={},
            attempts=decider.attempts,
            timeout=decider.timeout,
            commits=False,
            profile=decider.profile,
        )

    def _wait_for_human(self, ticket: TicketRuntime, decision: Decision) -> bool:
        """Puts the question to the owner when the Ticket enters a human Decision, and returns whether it did.

        The wait holds no slot and has no timeout, and the Ticket's Sandbox stays stopped until the answer comes.
        """
        if ticket.waiting is not None:
            return False
        card = self._write_card(ticket, decision)
        try:
            self.ports.sandbox.stop(self._sandbox_name(ticket))
        except SandboxError as error:
            self._append("SandboxStopFailed", {"ticket": ticket.id, "reason": str(error)})
        ticket.waiting = (decision.name, ticket.round)
        self._append(
            "DecisionWaiting",
            {
                "ticket": ticket.id,
                "node": decision.name,
                "round": ticket.round,
                "card": str(card),
                "outcomes": list(decision.outcomes),
            },
        )
        self.view.ring()
        return True

    def _write_card(self, ticket: TicketRuntime, decision: Decision) -> Path:
        """Renders the question card of the Decision's Round as an Artifact next to its outcome."""
        title = next(t.title for t in self.snapshot.tickets if t.id == ticket.id)
        cause = None
        if ticket.cause is not None:
            cause = Link(f"{ticket.cause['kind']} {ticket.cause['node']}", Path(ticket.cause["path"]))
        verdicts = {}
        for artifact in ticket.artifacts:
            if artifact["kind"] == "gate":
                verdicts[artifact["node"]] = Link(f"gate {artifact['node']}", Path(artifact["path"]))
        inputs = [
            Link(entry["source"] if "node" not in entry else f"{entry['source']} {entry['node']}", Path(entry["path"]))
            for item in decision.inputs
            for entry in self._resolve(ticket, item)
        ]
        outcomes = [
            CardOutcome(name, decision.descriptions.get(name, ""), self.pipeline.sends_back_to_work(decision, name))
            for name in decision.outcomes
        ]
        text = render_card(
            self.run_id, ticket.id, title, decision.name, cause, list(verdicts.values()), inputs, outcomes
        )
        return write_once(self.run_dir.decision(ticket.id, decision.name, ticket.round).with_name("question.md"), text)

    def _apply_commands(self) -> bool:
        """Accepts or rejects each Command in the inbox of the Run, oldest first, and returns whether any came."""
        commands = self.journal.pending_commands(self.run_id)
        for command in commands:
            try:
                ticket, decision, ruling = self._check_answer(command)
            except Rejected as rejection:
                self._append("CommandRejected", {"command": command.id, "kind": command.kind, "reason": str(rejection)})
                continue
            self._carry_out_answer(command, ticket, decision, ruling)
        return bool(commands)

    def _check_answer(self, command: CommandRow) -> tuple[TicketRuntime, Decision, deciders.Ruling]:
        """Returns the Ticket, Decision and Ruling a `decide` Command asks for, or raises Rejected with the reason."""
        if command.kind != "decide":
            raise Rejected(f"unknown command {command.kind}")
        payload = command.payload
        ticket = next((t for t in self.tickets if t.id == payload["ticket"]), None)
        if ticket is None:
            raise Rejected(f"no Ticket {payload['ticket']} in the Run")
        if payload["node"] is None:
            if ticket.answered:
                raise Rejected("already decided")
            raise Rejected(f"the answer names no Decision, as Ticket {ticket.id} waited for none when it was sent")
        if (payload["node"], payload["round"]) != ticket.waiting:
            raise Rejected("already decided")
        decision = self.pipeline.nodes[ticket.waiting[0]]
        assert isinstance(decision, Decision)
        outcome, reasons = payload["outcome"], payload["reasons"]
        if outcome not in decision.outcomes:
            raise Rejected(f"outcome {outcome!r} is not one of {', '.join(decision.outcomes)}")
        if self.pipeline.sends_back_to_work(decision, outcome) and not reasons:
            raise Rejected(f"outcome {outcome} sends the Ticket back to work and needs reasons")
        return ticket, decision, deciders.Ruling(outcome, reasons)

    def _carry_out_answer(
        self, command: CommandRow, ticket: TicketRuntime, decision: Decision, ruling: deciders.Ruling
    ) -> None:
        """Journals the accepted answer with the Decision's outcome in one transaction, then takes its route.

        The Sandbox starts again unless the route ends the Ticket.
        """
        target = self.pipeline.route(decision, ruling.outcome)
        result = f"Ticket {ticket.id} decided {decision.name}: {ruling.outcome}, goes to {target}"
        with self.journal.transaction():
            self._append(
                "CommandAccepted",
                {"command": command.id, "kind": command.kind, "ticket": ticket.id, "result": result},
            )
            outcome, artifact = self._record_decision(ticket, decision, ruling)
        ticket.waiting, ticket.answered = None, True
        if target not in TERMINAL_NODES:
            try:
                self.ports.sandbox.start(self._sandbox_name(ticket))
            except SandboxError as error:
                self._append("SandboxStartFailed", {"ticket": ticket.id, "reason": str(error)})
        self._leave(ticket, decision, outcome, artifact)

    def _call_function(self, ticket: TicketRuntime, decision: Decision) -> deciders.Ruling:
        """Runs the function of a function Decider on the outputs of the Stages the Decision reads."""
        assert decision.decider.function is not None
        outputs = []
        for item in decision.inputs:
            for entry in self._resolve(ticket, item):
                if entry["source"] == "stage":
                    output = json.loads(Path(entry["path"]).read_text())
                    outputs.append(output if isinstance(output, dict) else {})
        return deciders.decide(decision.decider.function, decision.decider.with_, outputs)

    def _model_ruling(self, ticket: TicketRuntime, finished: tuple[str, dict[str, Any] | None]) -> deciders.Ruling:
        """Returns what a model Decider decided, or `escalate` when every Attempt of its Round failed."""
        outcome, artifact = finished
        if outcome != "done" or artifact is None:
            return deciders.Ruling("escalate", ["the model Decider failed all its Attempts"])
        output = json.loads(Path(artifact["path"]).read_text())
        return deciders.Ruling(output["outcome"], list(output.get("reasons", [])))

    def _record_decision(
        self, ticket: TicketRuntime, decision: Decision, ruling: deciders.Ruling
    ) -> tuple[str, dict[str, Any]]:
        """Writes the Decision's outcome with its reasons as an Artifact, and journals it.

        The Orchestrator carries the outcome out by its route, and the artifact is the `cause` of the next node.
        """
        path = write_json_once(
            self.run_dir.decision(ticket.id, decision.name, ticket.round),
            {
                "decision": decision.name,
                "decider": decision.decider.kind,
                "outcome": ruling.outcome,
                "reasons": ruling.reasons,
            },
        )
        key = {"ticket": ticket.id, "node": decision.name, "round": ticket.round}
        self._append(
            "DecisionDecided",
            {
                **key,
                "decider": decision.decider.kind,
                "outcome": ruling.outcome,
                "reasons": ruling.reasons,
                "path": str(path),
            },
        )
        artifact = {"kind": "decision", "node": decision.name, "round": ticket.round, "path": str(path)}
        ticket.artifacts.append(artifact)
        return ruling.outcome, artifact

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

        A lost Probe gets no verdict and spends no Round or attempt of the Gate, and the Gate starts another on the
        next tick.
        Once the Ticket has lost its Sandbox more often than `sandbox_retries` the Gate escalates.
        The Workspace goes back to its committed state, as after a finished Probe.
        """
        assert ticket.workspace is not None
        ticket.probe = None
        ticket.gate_attempt -= 1
        ticket.sandbox_losses += 1
        self._append("ProbeLost", {**key, "reason": reason})
        self.git.reset(ticket.workspace, ticket.branch, ignored=False)
        if self._retries_exhausted(ticket):
            self._leave(ticket, gate, "escalate", None)

    def _retries_exhausted(self, ticket: TicketRuntime) -> bool:
        return ticket.sandbox_losses > self.pipeline.sandbox_retries

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
        ticket.probe_results[gate.name] = (running.key["probe"], probe)
        return self._finish_gate(ticket, gate, probe, running.dir.verdict, running.key)

    def _judge_borrowed(self, ticket: TicketRuntime, gate: Gate, source: str) -> tuple[str, dict[str, Any]]:
        """Judges a Gate with `from` on the latest Probe of its source Gate, and runs no Probe of its own.

        The source Gate judged the Probe's exit code and timeout, so this verdict reads only its reports.
        Config makes every path pass through the source, so the Workspace is as the source's Probe left it.
        """
        assert source in ticket.probe_results, "Config proves that every path to a Gate passes through its from"
        number, probe = ticket.probe_results[source]
        ticket.gate_attempt += 1
        key = {"ticket": ticket.id, "node": gate.name, "attempt": ticket.gate_attempt, "from": source, "probe": number}
        verdict = self.run_dir.borrowed_verdict(ticket.id, gate.name, source, number)
        return self._finish_gate(ticket, gate, replace(probe, exit_code=0, timed_out=False), verdict, key)

    def _finish_gate(
        self, ticket: TicketRuntime, gate: Gate, probe: gates.ProbeResult, verdict_path: Path, key: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        if gates.KINDS[gate.kind].judges_changed_lines:
            assert ticket.workspace is not None
            changed = self.git.changed_lines(ticket.workspace, ticket.branch, ticket.synced)
            probe = replace(probe, changed=changed)
        verdict = gates.judge(gate.name, gate.kind, gate.with_, probe)
        path = write_json_once(verdict_path, verdict)
        payload = {**key, "verdict": str(path), "outcome": verdict["outcome"], "reasons": verdict["reasons"]}
        self._append("GateFinished", payload)
        source = {"from": key["from"]} if "from" in key else {}
        artifact = {"kind": "gate", "node": gate.name, **source, "probe": key["probe"], "path": str(path)}
        ticket.artifacts.append(artifact)
        return str(verdict["outcome"]), artifact

    def _operate(self, ticket: TicketRuntime, operation: Operation) -> tuple[str, dict[str, Any] | None]:
        operate = {"sync": self._sync, "merge": self._merge}[operation.kind]
        return operate(ticket, operation, self.git.resolve(self.integration))

    def _sync(self, ticket: TicketRuntime, operation: Operation, tip: str) -> tuple[str, dict[str, Any] | None]:
        """Brings the Integration branch's tip into the Ticket branch, and gives `clean` or `conflict`.

        A conflict stays in the Workspace as markers and `MERGE_HEAD` and leaves an artifact that lists
        the conflicting files and the Tickets merged since the Ticket's base.

        The tip cannot move while this Ticket holds the window, so the Gates after it see the tree `merge` lands.
        """
        assert ticket.workspace is not None
        try:
            after = self.git.catch_up(ticket.workspace, ticket.branch, tip)
        except MergeConflict as error:
            head = self.git.resolve(ticket.branch)
            ticket.conflict = Conflict(tip, error.files)
            artifact = self._conflict_artifact(ticket, operation, tip, head, error.files)
            self._operation_finished(ticket, operation, tip, "conflict", head, reason=str(error))
            return "conflict", artifact
        ticket.synced = tip
        self._operation_finished(ticket, operation, tip, "clean", after)
        return "clean", None

    def _conflict_artifact(
        self, ticket: TicketRuntime, operation: Operation, tip: str, head: str, files: list[str]
    ) -> dict[str, Any]:
        """Writes what `resolve` needs to know: the conflicting files and the Tickets merged after the Ticket's base.

        Each merged Ticket comes with its commit range and its rendered Ticket file.
        """
        merged = [
            {**asdict(landing), "ticket_file": str(self.run_dir.ticket(landing.ticket))}
            for landing in self.landings
            if not self.git.is_ancestor(landing.after, ticket.base)
        ]
        conflicts = sum(a["kind"] == "operation" and a["node"] == operation.name for a in ticket.artifacts)
        path = self.run_dir.ticket_dir(ticket.id) / operation.name / f"conflict-{conflicts + 1}.json"
        content = {"files": files, "base": ticket.base, "head": head, "tip": tip, "merged_since_base": merged}
        write_json_once(path, content)
        artifact = {"kind": "operation", "node": operation.name, "path": str(path)}
        ticket.artifacts.append(artifact)
        return artifact

    def _merge(self, ticket: TicketRuntime, operation: Operation, tip: str) -> tuple[str, dict[str, Any] | None]:
        """Fast-forwards the Integration branch to the Ticket branch's head, and gives `merged`."""
        head = self.git.resolve(ticket.branch)
        self.git.fast_forward(self.integration, head, tip)
        ticket.state, ticket.merged_commit = TicketState.MERGED, head
        self.landings.append(Landing(ticket.id, tip, head))
        self._operation_finished(ticket, operation, tip, "merged", head)
        return "merged", None

    def _operation_finished(
        self, ticket: TicketRuntime, operation: Operation, before: str, outcome: str, after: str, **extra: str
    ) -> None:
        payload = {"ticket": ticket.id, "node": operation.name, "kind": operation.kind, "before": before}
        self._append("OperationFinished", {**payload, "outcome": outcome, "after": after, **extra})

    def _abandon(self, ticket: TicketRuntime) -> None:
        """Saves the head of an abandoned Ticket's branch under its ref, then cleans the Ticket up.

        A head that cannot be saved is journaled with its reason and keeps the Sandbox, Workspace and branch,
        because the branch would be the only place left to find the work.
        """
        assert ticket.workspace is not None
        ref = abandoned_ref(self.run_id, ticket.id)
        try:
            head = self.git.resolve(ticket.branch)
            self.git.keep(ref, head)
        except GitError as error:
            self._cleanup_failed(ticket, "ref", error)
            return
        ticket.kept = True
        self._append("AbandonedBranchSaved", {"ticket": ticket.id, "ref": ref, "commit": head})
        self._clean_up(ticket, head)

    def _clean_up(self, ticket: TicketRuntime, commit: str) -> None:
        """Removes the Sandbox, the Workspace and the branch of a merged or abandoned Ticket, in that order.

        A step that fails is journaled with its reason and keeps what the steps after it would remove,
        and the Ticket stays merged.
        A Sandbox that stays behind stopped for good runs nothing more, so the Workspace and branch still go.
        The branch goes only while it still points at `commit`, the merged or saved one,
        so no later commit is lost with it.
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
            self.git.delete_branch(ticket.branch, commit)
        except GitError as error:
            self._cleanup_failed(ticket, "branch", error)
            return
        self._append("TicketBranchDeleted", {"ticket": ticket.id, "branch": ticket.branch, "commit": commit})

    def _cleanup_failed(self, ticket: TicketRuntime, step: str, error: Exception) -> None:
        self._append("CleanupFailed", {"ticket": ticket.id, "step": step, "reason": str(error)})

    def _sandbox_name(self, ticket: TicketRuntime) -> str:
        return f"{self.run_id}-{ticket.id}"

    def _attempt_key(self, ticket: TicketRuntime) -> dict[str, Any]:
        return {"ticket": ticket.id, "node": ticket.node, "round": ticket.round, "attempt": ticket.attempt}

    def _append(self, type: str, payload: dict[str, Any]) -> None:
        self.journal.append(self.run_id, type, payload)
