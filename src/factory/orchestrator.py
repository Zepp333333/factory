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
from factory.commands import RUN_LEVEL
from factory.config import Project
from factory.executor import AttemptHandle, AttemptRequest, AttemptResult, Executor, Failure
from factory.git import GitError, HostGit, MergeConflict
from factory.journal import DESCRIBER_SLOT, CommandRow, Journal
from factory.merge_window import MergeWindow
from factory.pipeline import TERMINAL_NODES, Decision, Describer, Gate, Input, Node, Operation, Role, Stage
from factory.pipeline_checks import merges_without_sync
from factory.question import CardOutcome, Link, render_card, render_run_card
from factory.recovery import Conflict, Replay, replay
from factory.run_view import RunView
from factory.sandbox import Mount, Process, Sandbox, SandboxError, SandboxLost
from factory.states import SANDBOX_FAILURES, RunState, TicketState
from factory.tracker import PullRequest, PullRequestFolder, PullRequests, SpecSnapshot, Tracker, TrackerError
from factory.workbench import TERMINAL_LOG, AttemptTerminal, Workbench, WorkbenchError

TICK_SECONDS = 0.2

# The Run beats this often, in seconds, and another Orchestrator orphans a Run that has not beaten for the stale time.
HEARTBEAT_SECONDS = 10
HEARTBEAT_STALE_SECONDS = 60

# Reconciliation checks the world of the running Tickets this often, in seconds.
RECONCILE_SECONDS = 5

# A rate limit that gives no reset time still in the future pauses new Attempts for this many seconds.
RATE_LIMIT_PAUSE = 600

# Attempts of two Tickets that fail in a row with one of these causes open a Decision of the Run.
INFRA_FAILURES: tuple[Failure, ...] = ("auth", "sandbox_start", "executor_crash", "timeout")
RUN_DECISION = "infra_failure"
RUN_OUTCOMES = {
    "retry": "Start the Attempts again; the failed ones spent no Round or attempt",
    "abort": "End the Run as aborted",
}

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


def describer_names(home: Path, run_id: str) -> tuple[str, str, Path]:
    """Returns the Sandbox, branch and Workspace of the Describer, none of which a Ticket's can equal.

    Those of a Ticket start with the Run id or sit under its directory, and a Run id starts with a digit.
    """
    return (
        f"describer-{run_id}",
        f"factory/describer-{run_id}",
        home / "workspaces" / f"describer-{run_id}" / "workspace",
    )


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


class RunTakenOver(Exception):
    """A RunTakenOver means another Orchestrator orphaned this Run, so this process must stop writing to it."""


class Rejected(Exception):
    """A Rejected is the reason the Orchestrator turns a Command down."""


@dataclass(frozen=True)
class Landing:
    """A Landing is one `merge` of a Ticket, which moved the Integration branch from `before` to `after`."""

    ticket: str
    before: str
    after: str


@dataclass(frozen=True)
class Override:
    """An Override is the owner's new outcome for the Decision of `round`, which `replaces` an outcome already made.

    A Decision still waiting has nothing to replace, and keeps the usual `path` of its outcome.
    """

    round: int
    path: Path | None = None
    replaces: str | None = None


@dataclass(frozen=True)
class InfraFailure:
    """An InfraFailure is a failed Attempt of an infrastructure cause class, in the Round of `node` it failed in."""

    ticket: str
    cause: Failure
    node: str | None
    round: int


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
    `waiting_head` is the head of the Ticket branch when the Ticket began to wait, which the owner's commits build on.
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
    waiting_head: str = ""
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
        self.failures: list[InfraFailure] = []
        self.run_waiting: tuple[str, int] | None = None
        self.run_asked = 0
        self.aborting: str | None = None
        self.starved = False
        self.window = MergeWindow(self._append)
        self.max_parallel = project.max_parallel
        self.round_limits: dict[str, int] = {}
        self.landings: list[Landing] = []
        self.beat: datetime | None = None
        self.reconciled: datetime | None = None

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
        self.beat = self.ports.clock.now()
        self._append("SpecSnapshotted", {"snapshot": snapshot.to_json()})
        self._append("SpecRendered", self.run_dir.render(snapshot))
        self.git.create_branch(self.integration, base_commit)
        self._append(
            "IntegrationBranchCreated",
            {"branch": self.integration, "base_ref": self.project.base, "commit": base_commit},
        )
        self.tickets = [TicketRuntime(id=t.id, blocked_by=t.blocked_by) for t in snapshot.tickets]

    def resume(self, run_id: str, tracker: Tracker) -> None:
        """Takes up an orphaned Run from its events and puts its Tickets back where the journal says they were.

        An agent that outlived the dead Orchestrator is stopped and never adopted, so the Sandbox is stopped
        and the Attempt is lost; the Workspace goes back to the commit the Stage began at, and a new Attempt follows.
        A Probe in flight starts again.
        A Decision that waits stays waiting, and its Command in the inbox is applied on the first tick.
        """
        state = self._reclaim(run_id, tracker)
        for ticket in self.tickets:
            if ticket.state == TicketState.ACTIVE:
                self._take_over(ticket, state.in_flight.get(ticket.id), ticket.id in state.probing)

    def _reclaim(self, run_id: str, tracker: Tracker) -> Replay:
        """Takes an orphaned Run back from its events and starts beating for it, and returns what the events say."""
        state = self._reload(run_id, tracker)
        self.started = True
        self._append("RunReclaimed", {"in_flight": sorted(state.in_flight), "probing": sorted(state.probing)})
        self.beat = self.ports.clock.now()
        return state

    def _reload(self, run_id: str, tracker: Tracker) -> Replay:
        """Rebuilds the Run's Tickets and Merge window from its events and returns the rest of what they say."""
        events = self.journal.events(run_id)
        self.run_id = run_id
        self.integration = integration_branch(run_id)
        self.run_dir = RunDir(self.home / "runs" / run_id)
        self.tracker = tracker
        self.snapshot = SpecSnapshot.from_json(
            next(e.payload["snapshot"] for e in events if e.type == "SpecSnapshotted")
        )
        self.base_commit = next(e.payload["commit"] for e in events if e.type == "IntegrationBranchCreated")
        self.tickets = [TicketRuntime(id=t.id, blocked_by=t.blocked_by) for t in self.snapshot.tickets]
        state = replay(self.pipeline, self.tickets, events)
        self.landings = [Landing(*landing) for landing in state.landings]
        self.window.restore(state.holder, state.queue)
        self.paused_until, self.run_waiting, self.run_asked = state.paused_until, state.run_waiting, state.run_asked
        self.max_parallel = state.max_parallel or self.max_parallel
        self.round_limits = state.round_limits
        return state

    def finish(self, run_id: str, tracker: Tracker) -> RunState:
        """Ends an orphaned Run as `partial` with what it merged, and lands that.

        No session of an unfinished Ticket comes back: each Ticket that did not merge is abandoned,
        with its unsaved work kept under its ref, and the Run lands like any Run that ends `partial`.
        """
        state = self._reclaim(run_id, tracker)
        for ticket in self.tickets:
            if ticket.state in (TicketState.QUEUED, TicketState.ACTIVE):
                self._give_up(ticket, state.in_flight.get(ticket.id), ticket.id in state.probing)
        self._land()
        self._finish(RunState.PARTIAL)
        return RunState.PARTIAL

    def _give_up(self, ticket: TicketRuntime, attempt: dict[str, Any] | None, probing: bool) -> None:
        """Abandons a Ticket the owner finished the Run without.

        The agent its dead Orchestrator left is stopped first, and what it left uncommitted in the Workspace is
        committed to the Ticket branch, so the ref `_abandon` saves holds it.
        """
        was_active = ticket.state == TicketState.ACTIVE
        if was_active:
            self._save_unsaved(ticket, attempt, probing)
        ticket.state = TicketState.ABANDONED
        self._append("NodeEntered", {"ticket": ticket.id, "node": "abandoned", "reason": "the owner finished the Run"})
        self._abandon(ticket)

    def _save_unsaved(self, ticket: TicketRuntime, attempt: dict[str, Any] | None, probing: bool) -> None:
        """Stops the agent a dead Orchestrator left for a Ticket and commits what it left in the Workspace."""
        assert ticket.workspace is not None
        self._stop_leftover(ticket, attempt, probing)
        trailers = {"Factory-Run": self.run_id, "Factory-Ticket": ticket.id, "Factory-Actor": "finish"}
        try:
            commit = self.git.commit_all(ticket.workspace, ticket.branch, f"Unsaved work of {ticket.id}", trailers)
        except (GitError, OSError) as error:
            self._cleanup_failed(ticket, "unsaved", error)
            return
        if commit is not None:
            self._append("UnsavedWorkCommitted", {"ticket": ticket.id, "commit": commit})

    def _take_over(self, ticket: TicketRuntime, attempt: dict[str, Any] | None, probing: bool) -> None:
        """Brings one active Ticket of a resumed Run back: stops what its dead Orchestrator left running in its Sandbox,
        loses the Attempt in flight and mends what the world lacks.
        """
        assert ticket.workspace is not None
        self._stop_leftover(ticket, attempt, probing)
        mended = self._mend_world(ticket, attempt["head_before"] if attempt else self._known_head(ticket))
        if probing:
            self.git.reset(ticket.workspace, ticket.branch, ignored=False)
            self._append(
                "ProbeInterrupted", {"ticket": ticket.id, "node": ticket.node, "reason": "the Run was orphaned"}
            )
        if attempt is not None:
            attempt_dir = Path(attempt["stream"]).parent
            discarded = self._discard_to(ticket, attempt["head_before"], attempt_dir)
            self._lose_attempt(ticket, attempt, "orchestrator_lost", "the Run's process died", mended, discarded)

    def _stop_leftover(self, ticket: TicketRuntime, attempt: dict[str, Any] | None, probing: bool) -> None:
        """Stops the agent or Probe a dead Orchestrator left running in the Ticket's Sandbox."""
        assert ticket.workspace is not None
        try:
            name = self._sandbox_name(ticket)
            if (attempt or probing) and self.ports.sandbox.attach(name, ticket.workspace):
                self.ports.sandbox.stop(name)
        except SandboxError as error:
            self._append("SandboxStopFailed", {"ticket": ticket.id, "reason": str(error)})

    def _reconcile(self) -> bool:
        """Checks the world of the active Tickets against the journal now and then, and returns whether it found drift.

        A Ticket whose Sandbox, Workspace or branch is gone loses its running Attempt, and the world is made again.
        """
        now = self.ports.clock.now()
        if self.reconciled is not None and (now - self.reconciled).total_seconds() < RECONCILE_SECONDS:
            return False
        self.reconciled = now
        drift = False
        for ticket in self.tickets:
            if ticket.state == TicketState.ACTIVE and ticket.waiting is None:
                drift |= self._reconcile_ticket(ticket)
        return drift

    def _reconcile_ticket(self, ticket: TicketRuntime) -> bool:
        """Mends the Ticket's world if it lacks any of it and returns whether it did, losing the running Attempt."""
        if ticket.workspace is None or not (missing := self._missing(ticket)):
            return False
        running, ticket.running = ticket.running, None
        if ticket.probe is not None:
            ticket.probe.process.kill()
            ticket.probe = None
        if running is not None:
            running.handle.kill()
            self._close_terminal(running.terminal)
        mended = self._mend_world(ticket, running.head_before if running else self._known_head(ticket))
        if running is None:
            self._append("WorldMended", {"ticket": ticket.id, "mended": mended})
            return True
        key = {**self._attempt_key(ticket), "head_before": running.head_before}
        cause = {"sandbox": "sandbox_lost", "workspace": "workspace_lost", "branch": "branch_lost"}[missing[-1]]
        reason = f"the {', '.join(missing)} of the Ticket was gone"
        self._lose_attempt(ticket, key, cause, reason, mended, self._discard(ticket, running))
        return True

    def _lose_attempt(
        self,
        ticket: TicketRuntime,
        attempt: dict[str, Any],
        cause: str,
        reason: str,
        mended: list[str],
        extra: dict[str, Any],
    ) -> None:
        """Journals that the Attempt `attempt` (its key and `head_before`) is lost, and spends none of the Stage's.

        The Stage starts another Attempt on the next tick.
        A lost Sandbox still counts toward the Ticket's `sandbox_retries`, and past it the node ends as if every Attempt
        of its Round failed: a Stage gives `failed` and a model Decider escalates.
        """
        key = {k: attempt[k] for k in ("ticket", "node", "round", "attempt", "head_before")}
        self._append("AttemptLost", {**key, "cause": cause, "reason": reason, "mended": mended, **extra})
        if cause not in SANDBOX_FAILURES:
            return
        ticket.sandbox_losses += 1
        if not self._retries_exhausted(ticket):
            return
        self._journal_retries_exhausted(ticket, cause, reason)
        assert ticket.node is not None
        node = self.pipeline.nodes[ticket.node]
        if isinstance(node, Decision):
            outcome, artifact = self._record_decision(ticket, node, self._model_ruling(ticket, ("failed", None)))
            self._leave(ticket, node, outcome, artifact)
        else:
            self._leave(ticket, node, "failed", None)

    def _missing(self, ticket: TicketRuntime) -> list[str]:
        """Names what the Ticket lacks of its branch, Workspace and Sandbox, the one every other depends on last."""
        assert ticket.workspace is not None
        missing = []
        if not self._branch_exists(ticket.branch):
            missing.append("branch")
        if not self.ports.workbench.has_workspace(self.project.repo, ticket.workspace):
            missing.append("workspace")
        if not self.ports.sandbox.attach(self._sandbox_name(ticket), ticket.workspace):
            missing.append("sandbox")
        return missing

    def _mend_world(self, ticket: TicketRuntime, head: str) -> list[str]:
        """Makes again whatever of the Ticket's branch, Workspace and Sandbox is gone, and returns its names.

        A branch that is gone is made again at `head`, the commit the Ticket's next Attempt starts from.
        """
        assert ticket.workspace is not None
        missing = self._missing(ticket)
        if "branch" in missing:
            self.git.create_branch(ticket.branch, head)
        if "workspace" in missing:
            self.ports.workbench.create_workspace(self.project.repo, ticket.workspace, ticket.branch)
        if "sandbox" in missing:
            self.ports.sandbox.create(self._sandbox_name(ticket), ticket.workspace, self._mounts(ticket))
        return missing

    def _known_head(self, ticket: TicketRuntime) -> str:
        """Returns the head of the Ticket's branch as the journal last knew it."""
        if ticket.waiting_head:
            return ticket.waiting_head
        return next((a["head_after"] for a in reversed(ticket.artifacts) if "head_after" in a), ticket.base)

    def _branch_exists(self, branch: str) -> bool:
        try:
            self.git.resolve(branch)
        except GitError:
            return False
        return True

    def run(self) -> RunState:
        """Ticks until no Ticket can move and returns the Run's final state."""
        self.view.show(self.journal, self.run_id)
        while True:
            moved = self._tick()
            if self.aborting is not None:
                self.abort(self.aborting)
                return RunState.ABORTED
            if moved:
                self.view.show(self.journal, self.run_id)
            elif self.starved or self.run_waiting or any(t.busy or t.waiting for t in self.tickets):
                self.ports.clock.sleep(TICK_SECONDS)
            elif self.paused_until is not None:
                until_reset = (self.paused_until - self.ports.clock.now()).total_seconds()
                self.ports.clock.sleep(min(until_reset, HEARTBEAT_SECONDS))
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
        if self._stop_run_work():
            self._finish(RunState.ABORTED, reason)

    def interrupt(self, reason: str) -> None:
        """Leaves a started Run the owner stopped, with Ctrl-C or `kill`, orphaned at once and its slots freed.

        The journal then looks as if the process had died, so `resume` continues the Run and `finish` ends it.
        """
        if self._stop_run_work():
            self._append("RunOrphaned", {"silent_seconds": 0, "reason": reason})

    def _stop_run_work(self) -> bool:
        """Kills the agents and Probes still running and returns whether this Orchestrator still owns the Run."""
        if not self.started:
            return False
        taken_over = (run := self.journal.run(self.run_id)) is not None and run.state != RunState.RUNNING
        for ticket in self.tickets:
            if ticket.running is not None:
                ticket.running.handle.kill()
                self._close_terminal(ticket.running.terminal)
            if ticket.probe is not None:
                ticket.probe.process.kill()
        return not taken_over

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
        write_once(self.run_dir.pull_request_title, f"{pull.title}\n")
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
                return PullRequest(title=str(result.output["title"]), body=body)
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
        title = str(result.output["title"])
        if not title.strip() or title != title.strip() or "\n" in title or "\r" in title:
            return "the title must be one line with no space around it"
        body = str(result.output["body"])
        missing = [name for name in PR_SECTIONS if not re.search(rf"^#+\s+{name}\s*$", body, re.MULTILINE)]
        missing += [name for name in PR_DANGER if name not in body]
        return f"the body lacks {', '.join(missing)}" if missing else ""

    def _describe_attempt(self, describer: Describer, attempt: int) -> AttemptResult:
        """Runs one Attempt of the Describer in a Workspace on a branch at the Integration branch's tip.

        Its changes are discarded, and the Workspace, the Sandbox and the branch are removed after it.
        A Sandbox or git step that fails ends the Attempt as `sandbox_start`.
        """
        name, branch, workspace = describer_names(self.home, self.run_id)
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
            self._beat()

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
                self._beat()
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

        Each step that ends is journaled, and a step that fails is journaled with its reason and leaves the steps
        after it undone.
        """
        try:
            left_behind = self.ports.sandbox.remove(name)
            if left_behind is None:
                self._append("DescriberSandboxRemoved", {"sandbox": name})
            else:
                self._append("DescriberSandboxLeftBehind", {"sandbox": name, "reason": left_behind})
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
        self._beat()
        moved = self._resume()
        self.view.prompt(self.journal, self.run_id)
        moved |= self._apply_commands()
        moved |= self._reconcile()
        self.starved = False
        for ticket in self.tickets:
            if ticket.state == TicketState.ACTIVE:
                self._mark_stage(ticket)
                moved |= self._advance(ticket)
        return moved | self._claim_next()

    def _beat(self) -> None:
        """Journals a heartbeat once the interval has passed, and orphans the other Runs whose beat went stale.

        A Run that finds itself orphaned was too slow to beat, and another process may be resuming it, so it stops.
        """
        now = self.ports.clock.now()
        if self.beat is not None and (now - self.beat).total_seconds() < HEARTBEAT_SECONDS:
            return
        run = self.journal.run(self.run_id)
        if run is None or run.state != RunState.RUNNING:
            raise RunTakenOver(f"Run {self.run_id} was {run.state if run else 'removed'} by another process")
        self._append("Heartbeat", {})
        self.beat = now
        self.journal.orphan_stale(HEARTBEAT_STALE_SECONDS, keep=self.run_id)

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

    def _held(self) -> bool:
        """Tells whether new Attempts wait, for a rate limit to reset or for the owner to answer the Run's Decision."""
        return self.paused_until is not None or self.run_waiting is not None

    def _hold_for_run(self, ticket: TicketRuntime, failure: Failure) -> bool:
        """Notes a failed Attempt, and returns whether the Run's Decision holds it, so it spends nothing.

        Attempts of two different Tickets that fail in a row with one infrastructure cause open that Decision.
        Another cause or a done Attempt ends the row, and Attempts that fail while the Decision waits join it.
        """
        if failure not in INFRA_FAILURES:
            self.failures = []
            return False
        if self.run_waiting is not None:
            return True
        if self.failures and self.failures[-1].cause != failure:
            self.failures = []
        self.failures.append(InfraFailure(ticket.id, failure, ticket.node, ticket.round))
        if len({f.ticket for f in self.failures}) < 2:
            return False
        self._ask_run(failure)
        return True

    def _ask_run(self, cause: Failure) -> None:
        """Opens the Run's Decision `retry | abort` and gives back what the Attempts of the row spent.

        Only the Attempts whose Ticket is still in the same Round have anything to give back.
        """
        for failed in self.failures:
            ticket = next(t for t in self.tickets if t.id == failed.ticket)
            if (ticket.node, ticket.round) != (failed.node, failed.round):
                continue
            if failed.cause in SANDBOX_FAILURES:
                ticket.sandbox_losses = max(0, ticket.sandbox_losses - 1)
            else:
                ticket.spent = max(0, ticket.spent - 1)
        self.run_asked += 1
        names = [f"Ticket {f.ticket} at {f.node}, Round {f.round}" for f in self.failures]
        card = write_once(
            self.run_dir.root / "decisions" / f"{RUN_DECISION}-{self.run_asked}.md",
            render_run_card(self.run_id, RUN_DECISION, cause, names, RUN_OUTCOMES),
        )
        self.run_waiting = (RUN_DECISION, self.run_asked)
        self._append(
            "DecisionWaiting",
            {
                "ticket": RUN_LEVEL,
                "node": RUN_DECISION,
                "round": self.run_asked,
                "card": str(card),
                "outcomes": list(RUN_OUTCOMES),
                "cause": cause,
                "tickets": sorted({f.ticket for f in self.failures}),
            },
        )
        self.view.ring()

    def _answer_run(self, command: CommandRow) -> None:
        """Accepts the answer to the Run's Decision: `retry` goes on, and `abort` ends the Run after the tick."""
        payload = command.payload
        if payload["node"] is None:
            if self.run_asked:
                raise Rejected("already decided")
            raise Rejected("the answer names no Decision, as the Run waited for none when it was sent")
        if (payload["node"], payload["round"]) != self.run_waiting:
            raise Rejected("already decided")
        outcome = payload["outcome"]
        if outcome not in RUN_OUTCOMES:
            raise Rejected(f"outcome {outcome!r} is not one of {', '.join(RUN_OUTCOMES)}")
        with self.journal.transaction():
            self._append(
                "CommandAccepted",
                {
                    "command": command.id,
                    "kind": command.kind,
                    "ticket": RUN_LEVEL,
                    "result": f"Run decided {RUN_DECISION}: {outcome}",
                },
            )
            self._append(
                "DecisionDecided",
                {
                    "ticket": RUN_LEVEL,
                    "node": RUN_DECISION,
                    "round": payload["round"],
                    "decider": "human",
                    "outcome": outcome,
                    "reasons": payload["reasons"],
                },
            )
        self.run_waiting, self.failures = None, []
        if outcome == "abort":
            self.aborting = "the owner aborted the Run after Attempts failed in a row"

    def _resume(self) -> bool:
        """Ends the pause once its reset time comes, and returns whether it did."""
        if self.paused_until is None or self.ports.clock.now() < self.paused_until:
            return False
        self._append("RunResumed", {"resets_at": self.paused_until.isoformat()})
        self.paused_until = None
        return True

    def _claim_next(self) -> bool:
        """Claims the Frontier's queued Tickets in order, up to `max_parallel` active ones, and returns whether it did.

        A Ticket is on the Frontier once all its blockers are merged, so one that depends on an abandoned Ticket
        never starts.
        Claiming stops at the first Ticket that finds no free slot, and waits out a pause like any new Attempt.
        """
        merged = {t.id for t in self.tickets if t.state == TicketState.MERGED}
        claimed = False
        for ticket in self.tickets:
            active = sum(1 for t in self.tickets if t.state == TicketState.ACTIVE)
            if active >= self.max_parallel or self._held():
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
                if self._held():
                    return False
                return self._start_attempt(ticket, node)
            result = self._poll(ticket.running, node)
            if result is None:
                return False
            if result.failure is not None and self._reconcile_ticket(ticket):
                return True
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
                    return not self._held() and self._start_attempt(ticket, stage)
                result = self._poll(ticket.running, stage)
                if result is None:
                    return False
                if result.failure is not None and self._reconcile_ticket(ticket):
                    return True
                finished = self._finish_attempt(ticket, stage, result, node.outcomes)
                if finished is None:
                    return True
                ruling = self._model_ruling(ticket, finished)
            outcome, artifact = self._record_decision(ticket, node, ruling)
        elif isinstance(node, Gate) and node.from_ is not None:
            outcome, artifact = self._evaluate_borrowed(ticket, node, node.from_)
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
            outcome, artifact = self._evaluate(ticket, node, probe)
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
            limit = self.round_limits.get(target, node.max_rounds) if isinstance(node, Stage) else None
            if limit is not None and count > limit:
                to = self.pipeline.route(node, "exhausted")
                self._append(
                    "RoundsExhausted",
                    {"ticket": ticket.id, "node": target, "rounds": limit, "outcome": "exhausted", "to": to},
                )
                self._leave(ticket, node, "exhausted", ticket.cause)
                return
            ticket.rounds_since_reset[target] = count
            ticket.rounds[target] = ticket.rounds.get(target, 0) + 1
        ticket.node = target
        ticket.gate_attempt = 0
        payload: dict[str, Any] = {"ticket": ticket.id, "node": target}
        if ticket.cause is not None:
            payload["cause"] = ticket.cause
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
        An Attempt held by the Run's Decision spends none either, and is replaced once the owner answers `retry`.
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
            elif self._hold_for_run(ticket, failure):
                return None
            elif failure in SANDBOX_FAILURES:
                ticket.sandbox_losses += 1
                if not self._retries_exhausted(ticket):
                    return None
                self._journal_retries_exhausted(ticket, failure, result.reason)
                return "failed", None
            else:
                ticket.spent += 1
            return ("failed", None) if ticket.spent >= stage.attempts else None

        assert result.output is not None
        self.failures = []
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
        return self._discard_to(ticket, running.head_before, running.request.attempt_dir)

    def _discard_to(self, ticket: TicketRuntime, head: str, attempt_dir: Path) -> dict[str, Any]:
        assert ticket.workspace is not None
        patch, files = self.git.discard(ticket.workspace, ticket.branch, head)
        if ticket.conflict is not None:
            # The merge is what `resolve` works on, so the next Attempt starts from the same conflict.
            self.git.begin_merge(ticket.workspace, ticket.branch, ticket.conflict.tip)
        if not patch:
            return {}
        path = write_once(attempt_dir / "discarded.patch", patch)
        return {"discarded": str(path), "files": files}

    def _schema_errors(self, role: Role, output: dict[str, Any] | None) -> list[str]:
        if output is None:
            return []
        schema = json.loads(role.output.read_text())
        schema_check = jsonschema.Draft202012Validator(schema)
        return [error.message for error in schema_check.iter_errors(output)]

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
        assert ticket.workspace is not None
        ticket.waiting_head = self.git.head(ticket.workspace, ticket.branch)
        self._append(
            "DecisionWaiting",
            {
                "ticket": ticket.id,
                "node": decision.name,
                "round": ticket.round,
                "card": str(card),
                "head": ticket.waiting_head,
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
                match command.kind:
                    case "decide" if command.payload["ticket"] == RUN_LEVEL:
                        self._answer_run(command)
                    case "decide":
                        ticket, decision, ruling = self._check_answer(command)
                        self._carry_out_answer(command, ticket, decision, ruling)
                    case "cancel":
                        self._cancel(command)
                    case "override":
                        self._override(command)
                    case "limits":
                        self._set_limits(command)
                    case _:
                        raise Rejected(f"unknown command {command.kind}")
            except Rejected as rejection:
                self._append("CommandRejected", {"command": command.id, "kind": command.kind, "reason": str(rejection)})
        return bool(commands)

    def _ticket_of(self, command: CommandRow) -> TicketRuntime:
        ticket = next((t for t in self.tickets if t.id == command.payload["ticket"]), None)
        if ticket is None:
            raise Rejected(f"no Ticket {command.payload['ticket']} in the Run")
        return ticket

    def _accept(self, command: CommandRow, ticket: str, result: str) -> None:
        self._append(
            "CommandAccepted", {"command": command.id, "kind": command.kind, "ticket": ticket, "result": result}
        )

    def _cancel(self, command: CommandRow) -> None:
        """Abandons a Ticket that is queued or at work: its Attempt or Probe is stopped and its work is saved.

        A Ticket that depends on it never starts, since it only starts once all its blockers are merged.
        """
        ticket = self._ticket_of(command)
        if ticket.state not in (TicketState.QUEUED, TicketState.ACTIVE):
            raise Rejected(f"Ticket {ticket.id} is already {ticket.state}")
        if ticket.state == TicketState.ACTIVE:
            self._stop_work(ticket, "cancelled", "the owner cancelled the Ticket")
        with self.journal.transaction():
            self._accept(command, ticket.id, f"Ticket {ticket.id} cancelled, abandoned")
            self._append("TicketCancelled", {"ticket": ticket.id, "node": ticket.node})
        self.window.forget(ticket.id)
        if ticket.state == TicketState.QUEUED:
            ticket.state = TicketState.ABANDONED
            return
        ticket.waiting = None
        self._enter(ticket, "abandoned")

    def _stop_work(self, ticket: TicketRuntime, cause: str, reason: str) -> None:
        """Stops the Attempt or Probe the Ticket has going and puts the Workspace back where it was committed.

        The Attempt is lost with `cause`, which spends none of the Stage's and no Round, and a merge that `sync`
        left in the Workspace is dropped.
        A Ticket that waits for the owner keeps the owner's edits in the Workspace.
        """
        assert ticket.workspace is not None
        running, ticket.running = ticket.running, None
        probe, ticket.probe = ticket.probe, None
        merging, ticket.conflict = ticket.conflict is not None, None
        if probe is not None:
            probe.process.kill()
            self._append("ProbeInterrupted", {"ticket": ticket.id, "node": ticket.node, "reason": reason})
        if running is not None:
            running.handle.kill()
            self._close_terminal(running.terminal)
            key = {**self._attempt_key(ticket), "head_before": running.head_before}
            self._lose_attempt(ticket, key, cause, reason, [], self._discard(ticket, running))
        elif probe is not None or merging:
            self.git.reset(ticket.workspace, ticket.branch, ignored=False)

    def _override(self, command: CommandRow) -> None:
        """Replaces the outcome of the Decision the Ticket is at, or else its last one, and takes the new route.

        What the Ticket was doing since that Decision is stopped, like the work of a cancelled Ticket.
        The new outcome gets an Artifact of its own next to the old one, which stays.
        """
        ticket = self._ticket_of(command)
        if ticket.state != TicketState.ACTIVE or ticket.node is None:
            raise Rejected(
                f"Ticket {ticket.id} is {ticket.state}, and only a Ticket at work has a Decision to override"
            )
        node = self.pipeline.nodes[ticket.node]
        previous: dict[str, Any] | None = None
        if isinstance(node, Decision):
            decision, round = node, ticket.round
        else:
            previous = next((a for a in reversed(ticket.artifacts) if a["kind"] == "decision"), None)
            if previous is None:
                raise Rejected(f"Ticket {ticket.id} has made no Decision to override")
            decision, round = self.pipeline.nodes[previous["node"]], previous["round"]  # type: ignore[assignment]
        assert isinstance(decision, Decision)
        outcome, reasons = command.payload["outcome"], command.payload["reasons"]
        if outcome not in decision.outcomes:
            raise Rejected(f"outcome {outcome!r} is not one of {', '.join(decision.outcomes)}")
        if self.pipeline.sends_back_to_work(decision, outcome) and not reasons:
            raise Rejected(f"outcome {outcome} sends the Ticket back to work and needs reasons")
        waited = self._take_owner_edits(ticket) if ticket.waiting is not None else None
        self._stop_work(ticket, "overridden", f"the owner overrode the Decision {decision.name}")
        override = Override(round)
        if previous is not None:
            earlier = sum(
                a["kind"] == "decision" and (a["node"], a["round"]) == (decision.name, round) for a in ticket.artifacts
            )
            override = Override(
                round,
                self.run_dir.decision(ticket.id, decision.name, round).with_name(f"override-{earlier}.json"),
                json.loads(Path(previous["path"]).read_text())["outcome"],
            )
        target = self.pipeline.route(decision, outcome)
        with self.journal.transaction():
            self._accept(
                command, ticket.id, f"Ticket {ticket.id} overrode {decision.name}: {outcome}, goes to {target}"
            )
            _, artifact = self._record_decision(ticket, decision, deciders.Ruling(outcome, reasons), waited, override)
        restart = ticket.waiting is not None and target not in TERMINAL_NODES
        ticket.waiting, ticket.answered = None, True
        if restart:
            try:
                self.ports.sandbox.start(self._sandbox_name(ticket))
            except SandboxError as error:
                self._append("SandboxStartFailed", {"ticket": ticket.id, "reason": str(error)})
        self.window.forget(ticket.id)
        self._leave(ticket, decision, outcome, artifact)

    def _set_limits(self, command: CommandRow) -> None:
        """Changes the Run's parallelism and the round limits of its Stages, which count from the next Round."""
        payload = command.payload
        max_parallel, rounds = payload.get("max_parallel"), payload.get("rounds", {})
        if max_parallel is not None and max_parallel > 1:
            pipeline = self.pipeline
            if merges := merges_without_sync(pipeline.nodes, pipeline.start, pipeline.escalate_to):
                source, merge = merges[0]
                raise Rejected(
                    f"{max_parallel} Tickets at once need a sync before merge, but {source} reaches {merge} without one"
                )
        for name in rounds:
            stage = self.pipeline.nodes.get(name)
            if not isinstance(stage, Stage) or stage.max_rounds is None:
                raise Rejected(f"{name} is no Stage with a round limit")
        with self.journal.transaction():
            changes = [] if max_parallel is None else [f"max_parallel {max_parallel}"]
            changes += [f"{name} rounds {limit}" for name, limit in rounds.items()]
            self._accept(command, "", f"Limits set: {', '.join(changes)}")
            self._append("LimitsChanged", {"max_parallel": max_parallel, "rounds": rounds})
        self.max_parallel = max_parallel or self.max_parallel
        self.round_limits = {**self.round_limits, **rounds}

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
        assert ticket.workspace is not None
        if not self.git.is_ancestor(ticket.waiting_head, self.git.head(ticket.workspace, ticket.branch)):
            raise Rejected("the history of the Workspace was rewritten while the Decision waited")
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
        waited = self._take_owner_edits(ticket)
        target = self.pipeline.route(decision, ruling.outcome)
        result = f"Ticket {ticket.id} decided {decision.name}: {ruling.outcome}, goes to {target}"
        with self.journal.transaction():
            self._append(
                "CommandAccepted",
                {"command": command.id, "kind": command.kind, "ticket": ticket.id, "result": result},
            )
            outcome, artifact = self._record_decision(ticket, decision, ruling, waited)
        ticket.waiting, ticket.answered = None, True
        if target not in TERMINAL_NODES:
            try:
                self.ports.sandbox.start(self._sandbox_name(ticket))
            except SandboxError as error:
                self._append("SandboxStartFailed", {"ticket": ticket.id, "reason": str(error)})
        self._leave(ticket, decision, outcome, artifact)

    def _take_owner_edits(self, ticket: TicketRuntime) -> dict[str, str]:
        """Commits what the owner left uncommitted in the Workspace while the Decision waited, as the human.

        Returns the `head_before` and `head_after` of the waiting, whose range holds the owner's commits.
        """
        assert ticket.workspace is not None
        trailers = {"Factory-Run": self.run_id, "Factory-Ticket": ticket.id, "Factory-Actor": "human"}
        self.git.commit_all(ticket.workspace, ticket.branch, f"Owner edits to {ticket.id}", trailers)
        waited = {"head_before": ticket.waiting_head, "head_after": self.git.head(ticket.workspace, ticket.branch)}
        if waited["head_after"] != waited["head_before"]:
            self._append("OwnerEditsCommitted", {"ticket": ticket.id, **waited})
        return waited

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
        self,
        ticket: TicketRuntime,
        decision: Decision,
        ruling: deciders.Ruling,
        waited: dict[str, str] | None = None,
        override: Override | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Writes the Decision's outcome with its reasons as an Artifact, and journals it.

        An `override` is the owner's, and is journaled as `DecisionOverridden`.

        The Orchestrator carries the outcome out by its route, and the artifact is the `cause` of the next node.
        The artifact of a human Decision also carries the `head_before..head_after` range of its waiting (ADR 0009),
        so the next Stage reads the owner's edits with `git diff` like an agent's.
        """
        round = override.round if override else ticket.round
        decider = "human" if override else decision.decider.kind
        written = write_json_once(
            override.path if override and override.path else self.run_dir.decision(ticket.id, decision.name, round),
            {
                "decision": decision.name,
                "decider": decider,
                "outcome": ruling.outcome,
                "reasons": ruling.reasons,
                **({"overrides": override.replaces} if override and override.replaces else {}),
                **(waited or {}),
            },
        )
        key = {"ticket": ticket.id, "node": decision.name, "round": round}
        artifact = {"kind": "decision", "node": decision.name, "round": round, "path": str(written)}
        artifact.update(waited or {})
        self._append(
            "DecisionOverridden" if override else "DecisionDecided",
            {
                **key,
                "decider": decider,
                "outcome": ruling.outcome,
                "reasons": ruling.reasons,
                "path": str(written),
                "artifact": artifact,
            },
        )
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

        A lost Probe gets no verdict and spends no Round, and the Gate starts another on the next tick.
        The next Probe takes the next `attempt` number, so no two `ProbeStarted` events of a Gate look alike.
        Once the Ticket has lost its Sandbox more often than `sandbox_retries` the Gate escalates.
        The Workspace goes back to its committed state, as after a finished Probe.
        """
        assert ticket.workspace is not None
        ticket.probe = None
        ticket.sandbox_losses += 1
        self._append("ProbeLost", {**key, "reason": reason})
        self.git.reset(ticket.workspace, ticket.branch, ignored=False)
        if self._retries_exhausted(ticket):
            self._journal_retries_exhausted(ticket, "probe_lost", reason)
            self._leave(ticket, gate, "escalate", None)

    def _retries_exhausted(self, ticket: TicketRuntime) -> bool:
        return ticket.sandbox_losses > self.pipeline.sandbox_retries

    def _journal_retries_exhausted(self, ticket: TicketRuntime, cause: str, reason: str | None) -> None:
        """Journals that the Ticket's node ends because its Sandbox was lost once too often, and names the last loss."""
        self._append(
            "SandboxRetriesExhausted",
            {
                "ticket": ticket.id,
                "node": ticket.node,
                "losses": ticket.sandbox_losses,
                "sandbox_retries": self.pipeline.sandbox_retries,
                "cause": cause,
                "reason": reason,
            },
        )

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

    def _evaluate(self, ticket: TicketRuntime, gate: Gate, probe: gates.ProbeResult) -> tuple[str, dict[str, Any]]:
        """Writes the verdict on the finished Probe and returns the Gate's outcome with the verdict as its artifact."""
        assert ticket.probe is not None
        running, ticket.probe = ticket.probe, None
        ticket.probe_results[gate.name] = (running.key["probe"], probe)
        return self._finish_gate(ticket, gate, probe, running.dir.verdict, running.key)

    def _evaluate_borrowed(self, ticket: TicketRuntime, gate: Gate, source: str) -> tuple[str, dict[str, Any]]:
        """Evaluates a Gate with `from` on the latest Probe of its source Gate, and runs no Probe of its own.

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
        if gates.KINDS[gate.kind].evaluates_changed_lines:
            assert ticket.workspace is not None
            changed = self.git.changed_lines(ticket.workspace, ticket.branch, ticket.synced)
            probe = replace(probe, changed=changed)
        verdict = gates.evaluate(gate.name, gate.kind, gate.with_, probe)
        path = write_json_once(verdict_path, verdict)
        source = {"from": key["from"]} if "from" in key else {}
        artifact = {"kind": "gate", "node": gate.name, **source, "probe": key["probe"], "path": str(path)}
        payload = {
            **key,
            "verdict": str(path),
            "outcome": verdict["outcome"],
            "reasons": verdict["reasons"],
            "artifact": artifact,
        }
        self._append("GateFinished", payload)
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
            self._operation_finished(
                ticket, operation, tip, "conflict", head, reason=str(error), artifact=artifact["path"]
            )
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
        A Ticket that never got a Workspace has nothing to save or clean up, and one whose branch still points at its
        base holds no work, so it gets no ref.
        """
        if ticket.workspace is None:
            self._append("AbandonedWithoutWorkspace", {"ticket": ticket.id})
            return
        ref = abandoned_ref(self.run_id, ticket.id)
        try:
            head = self.git.resolve(ticket.branch)
            if head == ticket.base:
                self._append("AbandonedWithoutWork", {"ticket": ticket.id, "commit": head})
                self._clean_up(ticket, head)
                return
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
