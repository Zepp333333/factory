"""The Orchestrator is the deterministic loop that drives one Run and is the only writer of its events."""

import json
import re
import secrets
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from factory import deciders, gates, values
from factory.artifacts import RunDir, write_json_once, write_once
from factory.attempt import (
    RATE_LIMIT_PAUSE as RATE_LIMIT_PAUSE,
)
from factory.attempt import (
    Attempts,
    CleanupEvents,
    Ended,
    Ports,
    attempt_key,
    gave_back,
    schema_errors,
)
from factory.clock import Clock
from factory.commands import RUN_LEVEL, BadCommand, Cancel, Decide, Limits, parse
from factory.commands import Override as OverrideCommand
from factory.config import Ci, Project
from factory.custody import HEARTBEAT_SECONDS, RunCustody
from factory.decision import Asking, Decisions, Override, Rejected, check_asked, check_ruling
from factory.events import Event
from factory.executor import AttemptResult, Failure
from factory.git import GitError, HostGit, MergeConflict
from factory.journal import CommandRow, Journal
from factory.merge_window import MergeWindow
from factory.names import abandoned_ref, integration_branch, sandbox_name, ticket_branch
from factory.pipeline import Decision, Gate, Node, Operation, Role, Stage
from factory.pipeline_checks import merges_without_sync
from factory.projection import Projection
from factory.question import render_ci_card, render_failed_logs, render_run_card
from factory.recovery import replay
from factory.run_state import (
    CI_DECISION,
    CiWait,
    InfraFailure,
    Landing,
    RunMemory,
    RunningProbe,
    TicketRuntime,
    apply,
)
from factory.run_view import RunView
from factory.sandbox import Mount, SandboxError, SandboxLost
from factory.sleep_block import SleepBlock
from factory.states import TERMINAL_NODES, RunState, TicketState
from factory.throttle import Braked, Throttle, parse_brake, parse_window
from factory.tracker import (
    FailedJob,
    PullRequest,
    PullRequestFolder,
    PullRequests,
    SpecSnapshot,
    Tracker,
    TrackerError,
)
from factory.workbench import WorkbenchError

TICK_SECONDS = 0.2

# Reconciliation checks the world of the running Tickets this often, in seconds.
RECONCILE_SECONDS = 5

# Attempts of two Tickets that fail in a row with one of these causes open a Decision of the Run.
RUN_DECISION = "infra_failure"
RUN_OUTCOMES = {
    "retry": "Start the Attempts again; the failed ones spent no Round or attempt",
    "abort": "End the Run as aborted",
}

# The Decision `ci_failed` offers these outcomes.
CI_OUTCOMES = {
    "recheck": "Wait for the CI of the PR's current head again, after you fixed or reran something",
    "accept": "End the Run as it is, with the red CI recorded",
}

# The Run reads the CI of its PR this often, in seconds.
CI_POLL_SECONDS = 30

# A delivery to the Tracker that failed is tried again after this many seconds.
TRACKER_RETRY_SECONDS = 30

# The PR body the Describer writes holds these sections, and `Merge Danger` holds both of these entries.
PR_SECTIONS = ("Summary", "Evidence", "Merge Danger")
PR_DANGER = ("Door", "Blast Radius")


def _shares(shares: dict[str, float]) -> str:
    return ", ".join(f"{name} {share}" for name, share in shares.items())


def new_run_id(clock: Clock) -> str:
    return f"{clock.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def describer_names(home: Path, run_id: str) -> tuple[str, str, Path]:
    """Returns the Sandbox, branch and Workspace of the Describer, none of which a Ticket's can equal.

    Those of a Ticket start with the Run id or sit under its directory, and a Run id starts with a digit.
    """
    return (
        f"describer-{run_id}",
        f"factory/describer-{run_id}",
        home / "workspaces" / f"describer-{run_id}" / "workspace",
    )


def landing_remote(git: HostGit, pull_requests: PullRequests) -> str:
    """Returns the first git remote, by name, that points at the repository the PR opens in."""
    for name, url in sorted(git.remotes().items()):
        if pull_requests.accepts(url):
            return name
    raise GitError(f"no git remote of {git.repo} points at {pull_requests.url()}")


def resolve_base(git: HostGit, base: str, pull_requests: PullRequests | PullRequestFolder) -> str | None:
    """Fetches `base` from the remote the Run pushes to and returns the commit the Integration branch starts at.

    It returns None when the project has no remote the PR lands through, and the Run then starts from the local `base`.
    It raises GitError when the fetch fails or when the local `base` holds commits the remote lacks,
    so local work is never dropped silently.
    """
    if isinstance(pull_requests, PullRequestFolder):
        return None
    try:
        remote = landing_remote(git, pull_requests)
    except GitError:
        return None
    fetched = git.fetch(remote, base)
    try:
        local = git.resolve(f"refs/heads/{base}")
    except GitError:
        return fetched
    ahead = git.missing_from(local, fetched)
    if ahead:
        raise GitError(
            f"{base} holds commits that {remote} lacks, so the Run would drop them; push them or reset {base}:\n"
            + "\n".join(f"  {line}" for line in ahead)
        )
    return fetched


class Orchestrator:
    def __init__(
        self,
        journal: Journal,
        project: Project,
        ports: Ports,
        home: Path,
        view: RunView,
        slots: int,
        pull_requests: PullRequests | PullRequestFolder,
    ) -> None:
        self.journal = journal
        self.pull_requests = pull_requests
        self.project = project
        self.pipeline = project.pipeline
        self.ports = ports
        self.home = home
        self.view = view
        self.slots = slots
        self.git = HostGit(project.repo)
        self.run_id = ""
        self.started = False
        self.memory = RunMemory([], project.max_parallel, throttle=dict(project.throttle))
        self.throttle = Throttle()
        self.pull_request = ""
        self.pushed = ""
        self.ci_note = ""
        self.by_hand: list[list[str]] = []
        self.aborting: str | None = None
        self.window = MergeWindow(self._append)
        self.attempts = Attempts(
            journal,
            self._append,
            project,
            self.pipeline,
            ports,
            self.git,
            self,
            slots,
            TICK_SECONDS,
            self._beat,
            self._ask_run,
        )
        self.decisions = Decisions(self._append, self.pipeline, ports, self.git, self, self.view.ring)
        self.custody = RunCustody(journal, ports.clock)
        self.reconciled: datetime | None = None
        self.retry_delivery_at: datetime | None = None

    @property
    def tickets(self) -> list[TicketRuntime]:
        return self.memory.tickets

    @property
    def paused_until(self) -> datetime | None:
        return self.memory.paused_until

    @property
    def braked(self) -> Braked | None:
        return self.memory.braked

    @property
    def failures(self) -> list[InfraFailure]:
        return self.memory.failures

    @property
    def run_waiting(self) -> tuple[str, int] | None:
        return self.memory.run_waiting

    @property
    def max_parallel(self) -> int:
        return self.memory.max_parallel

    @property
    def round_limits(self) -> dict[str, int]:
        return self.memory.round_limits

    @property
    def landings(self) -> list[Landing]:
        return self.memory.landings

    def start(self, run_id: str, snapshot: SpecSnapshot, tracker: Tracker, base_commit: str) -> None:
        """Starts the Run on the snapshot the Tracker gave, and lands it through the PullRequests the Run was given.

        The Integration branch begins at `base_commit`, which `resolve_base` found.
        """
        self.run_id = run_id
        self.integration = integration_branch(run_id)
        self.run_dir = RunDir(self.home / "runs" / run_id)
        self.snapshot = snapshot
        self.tracker = tracker
        self.base_commit = base_commit
        self.ports.executor.assemble(self.run_dir.skills, self.run_dir.kit)

        self._append(
            Event.RunStarted,
            {
                "spec_ref": snapshot.ref,
                "project": str(self.project.path),
                "pipeline": str(self.pipeline.path),
                "run_dir": str(self.run_dir.root),
                "integration_branch": self.integration,
                "max_parallel": self.max_parallel,
                **self.attempts.variant.run,
            },
        )
        self.started = True
        self.custody.begin()
        self._append(Event.SpecSnapshotted, {"snapshot": snapshot.to_json()})
        self._append(Event.SpecRendered, self.run_dir.render(snapshot))
        self.git.create_branch(self.integration, base_commit)
        self._append(
            Event.IntegrationBranchCreated,
            {"branch": self.integration, "base_ref": self.project.base, "commit": base_commit},
        )
        self.memory.tickets = [TicketRuntime(id=t.id, blocked_by=t.blocked_by) for t in snapshot.tickets]

    def journal_sleep_block(self, block: SleepBlock) -> None:
        """Journals that the Run keeps the computer awake, or why it cannot."""
        if block.reason is None:
            self._append(Event.SleepBlocked, {"argv": block.argv})
            return
        self._append(Event.SleepBlockUnavailable, {"reason": block.reason})

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

    def _reclaim(self, run_id: str, tracker: Tracker) -> RunMemory:
        """Takes an orphaned Run back from its events and starts beating for it, and returns what the events say."""
        state = self._reload(run_id, tracker)
        payload = {"in_flight": sorted(state.in_flight), "probing": sorted(state.probing)}
        self.custody.reclaim(run_id, payload)
        self.started = True
        apply(self.pipeline, self.memory, Event.RunReclaimed, payload)
        return state

    def _reload(self, run_id: str, tracker: Tracker) -> RunMemory:
        """Rebuilds the Run's Tickets and Merge window from its events and returns the rest of what they say."""
        events = self.journal.events(run_id)
        self.run_id = run_id
        self.integration = integration_branch(run_id)
        self.run_dir = RunDir(self.home / "runs" / run_id)
        self.ports.executor.assemble(self.run_dir.skills, self.run_dir.kit)
        self.tracker = tracker
        self.snapshot = SpecSnapshot.from_json(
            next(e.payload["snapshot"] for e in events if e.type == Event.SpecSnapshotted)
        )
        self.base_commit = next(e.payload["commit"] for e in events if e.type == Event.IntegrationBranchCreated)
        tickets = [TicketRuntime(id=t.id, blocked_by=t.blocked_by) for t in self.snapshot.tickets]
        self.memory = replay(self.pipeline, tickets, events, self.project.max_parallel, self.project.throttle)
        self.window.restore(self.memory.holder, self.memory.queue)
        self.pull_request = self.memory.pull_request
        return self.memory

    def finish(self, run_id: str, tracker: Tracker) -> RunState:
        """Ends an orphaned Run as `partial` with what it merged, and lands that.

        A Run orphaned in phase `ci` has landed already: it ends without waiting for the CI, which the journal records
        as unchecked, and its state is that of any Run whose Tickets all merged.
        No session of an unfinished Ticket comes back: each Ticket that did not merge is abandoned,
        with its unsaved work kept under its ref, and the Run lands like any Run that ends `partial`.
        """
        state = self._reclaim(run_id, tracker)
        for ticket in self.tickets:
            if ticket.state in (TicketState.QUEUED, TicketState.ACTIVE):
                self._give_up(ticket, state.in_flight.get(ticket.id), ticket.id in state.probing)
        self._deliver(last=True)
        self._land()
        if self.memory.ci is not None:
            # The PR is open and the owner ends the Run without waiting for its CI.
            self._append(Event.CiFinished, self._ci_payload("unchecked", self.memory.ci.commit, []))
            self.ci_note = "CI not checked"
            self._finish(self._final_state())
            return self._final_state()
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
        self._append(
            Event.NodeEntered,
            {
                "ticket": ticket.id,
                "node": "abandoned",
                "at": self.ports.clock.now().isoformat(),
                "reason": "the owner finished the Run",
            },
        )
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
            self._append(Event.UnsavedWorkCommitted, {"ticket": ticket.id, "commit": commit})

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
                Event.ProbeInterrupted, {"ticket": ticket.id, "node": ticket.node, "reason": "the Run was orphaned"}
            )
        if attempt is not None:
            attempt_dir = Path(attempt["stream"]).parent
            discarded = self.attempts.discard(ticket, attempt["head_before"], attempt_dir)
            self._lose_attempt(ticket, attempt, "orchestrator_lost", "the Run's process died", mended, discarded)

    def _stop_leftover(self, ticket: TicketRuntime, attempt: dict[str, Any] | None, probing: bool) -> None:
        """Stops the agent or Probe a dead Orchestrator left running in the Ticket's Sandbox."""
        assert ticket.workspace is not None
        try:
            name = self._sandbox_name(ticket)
            if (attempt or probing) and self.ports.sandbox.attach(name, ticket.workspace):
                self.ports.sandbox.stop(name)
        except SandboxError as error:
            self._append(Event.SandboxStopFailed, {"ticket": ticket.id, "reason": str(error)})

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
            self.attempts.kill(running)
        mended = self._mend_world(ticket, running.head_before if running else self._known_head(ticket))
        if running is None:
            self._append(Event.WorldMended, {"ticket": ticket.id, "mended": mended})
            return True
        key = {**attempt_key(ticket), "head_before": running.head_before}
        cause = {"sandbox": "sandbox_lost", "workspace": "workspace_lost", "branch": "branch_lost"}[missing[-1]]
        reason = f"the {', '.join(missing)} of the Ticket was gone"
        discarded = self.attempts.discard(ticket, running.head_before, running.request.attempt_dir)
        self._lose_attempt(ticket, key, cause, reason, mended, discarded)
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
        """Loses the Attempt `attempt` and ends the node if that was one loss too many for the Ticket's Sandbox."""
        ended = self.attempts.lose(ticket, attempt, cause, reason, mended, extra)
        if ended is not None:
            self._conclude(ticket, ended)

    def _conclude(self, ticket: TicketRuntime, ended: Ended) -> None:
        """Ends the node the Ticket is at after every Attempt of its Round failed: a Stage gives `failed`,
        and a model Decider escalates.
        """
        assert ticket.node is not None
        node = self.pipeline.nodes[ticket.node]
        if isinstance(node, Decision):
            outcome, artifact = self.decisions.record(ticket, node, self.decisions.model_ruling(ended))
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
                continue
            self.view.tick(self.journal, self.run_id)
            if self.attempts.starved or self.run_waiting or any(t.busy or t.waiting for t in self.tickets):
                self.ports.clock.sleep(TICK_SECONDS)
            elif waits := [t for t in (self.paused_until, self.braked and self.braked.until) if t]:
                until_reset = (min(waits) - self.ports.clock.now()).total_seconds()
                self.ports.clock.sleep(min(max(until_reset, TICK_SECONDS), HEARTBEAT_SECONDS))
            else:
                break
        self._deliver(last=True)
        state = self._final_state()
        self._land()
        self._await_ci()
        self._finish(state)
        return state

    def _final_state(self) -> RunState:
        finished = all(t.state in (TicketState.MERGED, TicketState.DONE) for t in self.tickets)
        return RunState.DONE if finished else RunState.PARTIAL

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
            self.custody.release(self.run_id, reason)

    def _stop_run_work(self) -> bool:
        """Kills the agents and Probes still running and returns whether this Orchestrator still owns the Run."""
        if not self.started:
            return False
        taken_over = not self.custody.holds(self.run_id)
        for ticket in self.tickets:
            if ticket.running is not None:
                self.attempts.kill(ticket.running)
            if ticket.probe is not None:
                ticket.probe.process.kill()
        return not taken_over

    def _land(self) -> None:
        """Writes the PR text of a Run that merged a Ticket, then pushes the Integration branch and opens its PR.

        The text is an Artifact in any case, and a Run with no git remote to land through records the PR in a folder.
        A Run that resumes after its PR opened lands nothing again.
        A step that fails is journaled with its reason and leaves the Run's state alone,
        and `by_hand` then holds the commands that finish the landing.
        """
        if self.memory.pull_request or not any(t.state == TicketState.MERGED for t in self.tickets):
            return
        states = {t.id: t.state for t in self.tickets}
        kept = {t.id: abandoned_ref(self.run_id, t.id) for t in self.tickets if t.kept}
        pull = self._describe(states, kept)
        body = write_once(self.run_dir.pull_request, pull.body)
        write_once(self.run_dir.pull_request_title, f"{pull.title}\n")
        pull_requests = self.pull_requests
        if isinstance(pull_requests, PullRequestFolder):
            self.pull_request = str(pull_requests.record(self.integration, self.project.base, pull.title, body))
            self._append(
                Event.PullRequestRecorded,
                {"path": self.pull_request, "head": self.integration, "base": self.project.base},
            )
            return
        open_command = pull_requests.open_command(self.integration, self.project.base, pull.title, body)
        remote = None
        try:
            commit = self.git.resolve(self.integration)
            self.pushed = commit
            remote = landing_remote(self.git, pull_requests)
            self.git.push(remote, self.integration)
        except GitError as error:
            self._append(Event.LandingFailed, {"step": "push", "reason": str(error)})
            self.by_hand = [["git", "push", remote or pull_requests.url(), self.integration], open_command]
            return
        self._append(Event.IntegrationBranchPushed, {"remote": remote, "branch": self.integration, "commit": commit})
        try:
            self.pull_request = pull_requests.open(self.integration, self.project.base, pull.title, body)
        except TrackerError as error:
            self._append(Event.LandingFailed, {"step": "pull_request", "reason": str(error)})
            self.by_hand = [open_command]
            return
        self._append(
            Event.PullRequestOpened,
            {"url": self.pull_request, "head": self.integration, "base": self.project.base, "body": str(body)},
        )

    def _await_ci(self) -> None:
        """Waits for the CI of the Run's final PR and records how it ended, unless the Run has no CI to wait for.

        It polls at a fixed interval while the Run beats, and holds no slot, because no agent works.
        A PR with no check on its head commit within `grace` takes the project as one without CI.
        Red CI, or CI that does not end within `timeout`, opens the Decision `ci_failed`, and the wait goes on
        until the owner answers it: `recheck` waits for the PR's current head, and `accept` ends the Run.
        A Run that resumes in this phase goes on waiting for the commit it waited for.
        """
        pull_requests = self.pull_requests
        ci = self.project.ci
        if ci is None or isinstance(pull_requests, PullRequestFolder) or not self.memory.pull_request:
            return
        wait = self.memory.ci
        if wait is None or not wait.failed:
            self._start_ci(pull_requests, wait.commit if wait else self.pushed)
        polled: datetime | None = None
        while self.memory.ci is not None:
            self._beat()
            self.view.prompt(self.journal, self.run_id)
            moved = self._apply_commands()
            if (wait := self.memory.ci) is None:
                break
            if wait.failed:
                if self.run_waiting is None:
                    self._start_ci(pull_requests, None)
                    polled = None
                elif not moved:
                    self.view.tick(self.journal, self.run_id)
                    self.ports.clock.sleep(TICK_SECONDS)
                continue
            now = self.ports.clock.now()
            if polled is None or (now - polled).total_seconds() >= CI_POLL_SECONDS:
                polled = now
                self._poll_ci(pull_requests, wait, ci)
                self.view.show(self.journal, self.run_id)
            else:
                self.view.tick(self.journal, self.run_id)
                self.ports.clock.sleep(HEARTBEAT_SECONDS)

    def _start_ci(self, pull_requests: PullRequests, commit: str | None) -> None:
        """Journals that the Run waits for the CI of `commit`, which is the PR's head now when none is given."""
        commit = commit or pull_requests.head(self.memory.pull_request)
        self._append(
            Event.CiStarted,
            {"commit": commit, "url": self.memory.pull_request, "at": self.ports.clock.now().isoformat()},
        )

    def _poll_ci(self, pull_requests: PullRequests, wait: CiWait, ci: Ci) -> None:
        """Reads the CI once and ends the wait when it passed, failed, never showed up or ran out of time."""
        status = pull_requests.ci(self.memory.pull_request, wait.commit)
        elapsed = (self.ports.clock.now() - wait.started).total_seconds()
        match status.state:
            case "passed":
                self._append(Event.CiFinished, self._ci_payload("passed", wait.commit, []))
                self.ci_note = "CI passed"
            case "failed":
                self._ci_failed("failed", wait.commit, status.jobs)
            case "none" if elapsed >= ci.grace:
                self._append(Event.CiAbsent, {"commit": wait.commit, "grace": ci.grace})
                self.ci_note = "no CI checks"
            case _ if elapsed >= ci.timeout:
                self._ci_failed("timeout", wait.commit, status.jobs)

    def _ci_failed(self, outcome: str, commit: str, jobs: list[FailedJob]) -> None:
        """Journals the red CI with its failed logs as an Artifact, and opens the Decision `ci_failed`."""
        number = self.memory.run_asked + 1
        log = None
        if jobs:
            log = write_once(self.run_dir.root / "ci" / f"failed-{number}.md", render_failed_logs(jobs))
        self._append(Event.CiFinished, self._ci_payload(outcome, commit, jobs, log))
        names = [f"{job.name}: {job.url}" for job in jobs]
        card = write_once(
            self.run_dir.root / "decisions" / f"{CI_DECISION}-{number}.md",
            render_ci_card(self.run_id, CI_DECISION, outcome, commit, names, log, CI_OUTCOMES),
        )
        self._append(
            Event.DecisionWaiting,
            {
                "ticket": RUN_LEVEL,
                "node": CI_DECISION,
                "round": number,
                "card": str(card),
                "outcomes": list(CI_OUTCOMES),
                "cause": f"ci_{outcome}",
                "tickets": [],
                "gave_back": [],
            },
        )
        self.view.ring()

    def _ci_payload(self, outcome: str, commit: str, jobs: list[FailedJob], log: Path | None = None) -> dict[str, Any]:
        failed = [{"name": job.name, "url": job.url} for job in jobs]
        return {"outcome": outcome, "commit": commit, "jobs": failed, **({"artifact": str(log)} if log else {})}

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
        names = describer_names(self.home, self.run_id)
        for attempt in range(1, describer.attempts + 1):
            tip = self.git.resolve(self.integration)
            result = self.attempts.describe(
                describer,
                attempt,
                names,
                tip,
                partial(self._describer_brief, attempt),
                self._describer_mounts,
            )
            problems = self._pull_request_problems(describer.role, result)
            if not problems:
                assert result.output is not None
                footer = self.tracker.footer(self.snapshot, states, kept)
                body = f"{self.tracker.defuse(str(result.output['body'])).strip()}\n\n{footer}"
                self._append(Event.PullRequestDescribed, {"attempt": attempt, "title": result.output["title"]})
                return PullRequest(title=str(result.output["title"]), body=body)
            cause = result.failure or "invalid_output"
            silent = {"silent": result.silent} if result.silent is not None else {}
            self._append(Event.DescriberFailed, {"attempt": attempt, "cause": cause, "reason": problems, **silent})
        return fallback

    def _pull_request_problems(self, role: Role, result: AttemptResult) -> str:
        """Tells why the Describer's result is no PR text, or returns an empty text when it is one."""
        if result.failure is not None:
            return result.reason or result.failure
        errors = schema_errors(role.output, result.output)
        if errors or result.output is None:
            return "; ".join(errors)
        title = str(result.output["title"])
        if not title.strip() or title != title.strip() or "\n" in title or "\r" in title:
            return "the title must be one line with no space around it"
        body = str(result.output["body"])
        missing = [name for name in PR_SECTIONS if not re.search(rf"^#+\s+{name}\s*$", body, re.MULTILINE)]
        missing += [name for name in PR_DANGER if name not in body]
        return f"the body lacks {', '.join(missing)}" if missing else ""

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
            Mount(self.run_dir.kit),
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

    def _finish(self, state: RunState, reason: str | None = None) -> None:
        self._append(Event.RunFinished, {"state": state, **({"reason": reason} if reason else {})})
        self.view.show(self.journal, self.run_id)

    def _tick(self) -> bool:
        self._beat()
        moved = self._resume()
        moved |= self._brake()
        self.view.prompt(self.journal, self.run_id)
        moved |= self._apply_commands()
        self._deliver()
        moved |= self._reconcile()
        self.attempts.starved = False
        for ticket in self.tickets:
            if ticket.state == TicketState.ACTIVE:
                self._mark_stage(ticket)
                moved |= self._advance(ticket)
        return moved | self._claim_next()

    def _deliver(self, last: bool = False) -> None:
        """Hands the Tracker the events after its cursor, and on failure journals why and waits before trying again.

        The `last` delivery at the end of the Run tries once whatever the wait, and a failure never stops the Run.
        """
        now = self.ports.clock.now()
        if not last and self.retry_delivery_at is not None and now < self.retry_delivery_at:
            return
        failure = Projection(self.journal, self.run_id, self.tracker, self._append).deliver()
        if failure is None:
            self.retry_delivery_at = None
            return
        self.retry_delivery_at = now + timedelta(seconds=TRACKER_RETRY_SECONDS)
        self._append(Event.TrackerDeliveryFailed, failure)

    def _beat(self) -> None:
        """Beats for the Run, which raises RunTakenOver when another process took the Run over."""
        self.custody.beat(self.run_id)

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

    def _held(self) -> bool:
        """Tells whether new Attempts wait, for a rate limit to reset, for the brake to let go,
        for the owner to answer the Run's Decision, or for the Run to end after the owner aborted it.
        """
        return (
            self.paused_until is not None
            or self.braked is not None
            or self.run_waiting is not None
            or self.aborting is not None
        )

    def _ask_run(self, cause: Failure) -> None:
        """Opens the Run's Decision `retry | abort` and gives back what the Attempts of the row charged.

        The Attempt module says what there is to give back, and the give-back is part of the event,
        so replay and `factory status` see it.
        """
        round = self.memory.run_asked + 1
        names = [f"Ticket {f.ticket} at {f.node}, Round {f.round}" for f in self.failures]
        card = write_once(
            self.run_dir.root / "decisions" / f"{RUN_DECISION}-{round}.md",
            render_run_card(self.run_id, RUN_DECISION, cause, names, RUN_OUTCOMES),
        )
        self._append(
            Event.DecisionWaiting,
            {
                "ticket": RUN_LEVEL,
                "node": RUN_DECISION,
                "round": round,
                "card": str(card),
                "outcomes": list(RUN_OUTCOMES),
                "cause": cause,
                "tickets": sorted({f.ticket for f in self.failures}),
                "gave_back": gave_back(self.memory),
            },
        )
        self.view.ring()

    def _answer_run(self, command: CommandRow, answer: Decide) -> None:
        """Accepts the answer to the Run's Decision.

        For `infra_failure`, `retry` goes on and `abort` ends the Run after the tick.
        For `ci_failed`, `recheck` waits for the CI again and `accept` ends the Run with the red CI recorded.
        """
        check_asked(Asking("the Run", self.run_waiting, bool(self.memory.run_asked)), answer.node, answer.round)
        assert self.run_waiting is not None
        decision = self.run_waiting[0]
        outcomes = CI_OUTCOMES if decision == CI_DECISION else RUN_OUTCOMES
        outcome = check_ruling(answer.outcome, answer.reasons, tuple(outcomes), lambda _: False).outcome
        if decision == CI_DECISION and outcome == "accept":
            ended = "timed out" if self.memory.ci and self.memory.ci.outcome == "timeout" else "failed"
            self.ci_note = f"CI {ended}, accepted by the owner"
        with self.journal.transaction():
            self._append(
                Event.CommandAccepted,
                {
                    "command": command.id,
                    "kind": command.kind,
                    "ticket": RUN_LEVEL,
                    "result": f"Run decided {decision}: {outcome}",
                },
            )
            self._append(
                Event.DecisionDecided,
                {
                    "ticket": RUN_LEVEL,
                    "node": decision,
                    "round": answer.round,
                    "decider": deciders.OWNER,
                    "outcome": outcome,
                    "reasons": answer.reasons,
                },
            )
        if outcome == "abort":
            self.aborting = "the owner aborted the Run after Attempts failed in a row"

    def _resume(self) -> bool:
        """Ends the pause once its reset time comes, and returns whether it did."""
        if self.paused_until is None or self.ports.clock.now() < self.paused_until:
            return False
        self._append(Event.RunResumed, {"resets_at": self.paused_until.isoformat()})
        return True

    def _brake(self) -> bool:
        """Turns the brake on when a window is used beyond its `cap` or `pace`, and off when it is within bounds again.

        A brake that stays on is journaled again only when another window holds the Run back or its time has passed,
        so the Journal does not log every change of usage.
        """
        now, windows = self.ports.clock.now(), self.ports.executor.windows()
        found = self.throttle.check(self.memory.throttle, windows, now)
        current = self.braked
        if found is None:
            if current is None:
                return False
            usage = windows.get(current.window)
            reset = usage is None or now >= usage.resets_at
            payload = {"window": current.window, "used": None if usage is None or reset else usage.used}
            self._append(Event.ThrottleOff, {**payload, "reason": "window reset" if reset else "within bounds"})
            return True
        if current is None or current.window != found.window or now >= current.until:
            self._append(Event.ThrottleOn, found.payload())
            return True
        return False

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
            if stage is not None and not self.attempts.take_slot(ticket.id):
                return False
            workspace = self.home / "workspaces" / self.run_id / ticket.id
            self._append(
                Event.TicketClaimed,
                {
                    "ticket": ticket.id,
                    "branch": ticket_branch(self.run_id, ticket.id),
                    "base": base,
                    "workspace": str(workspace),
                },
            )
            self._enter(ticket, self.pipeline.start, None)
            request = self.attempts.begin(ticket, stage, base) if stage is not None else None
        assert ticket.workspace is not None
        self.git.create_branch(ticket.branch, ticket.base)
        self.ports.workbench.create_workspace(self.project.repo, ticket.workspace, ticket.branch)
        self.ports.sandbox.create(self._sandbox_name(ticket), ticket.workspace, self._mounts(ticket))
        if request is not None:
            self.attempts.launch(ticket, request, base)
        return True

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
            Mount(self.run_dir.kit),
            *(Mount(path) for path in self.ports.executor.reads()),
        ]

    def _advance(self, ticket: TicketRuntime) -> bool:
        assert ticket.node is not None
        node = self.pipeline.nodes[ticket.node]
        if isinstance(node, Stage):
            ended = self._step_attempt(ticket, node)
            if not isinstance(ended, Ended):
                return ended
            outcome, artifact = ended.outcome, ended.artifact
            assert outcome is not None
        elif isinstance(node, Decision):
            decided = self.decisions.decide(ticket, node, lambda: self._step_attempt(ticket, node))
            if not isinstance(decided, tuple):
                return decided
            outcome, artifact = decided
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

    def _step_attempt(self, ticket: TicketRuntime, node: Stage | Decision) -> Ended | bool:
        """Starts or steps the Attempt of a node, and returns whether the Ticket moved, or how the node ended.

        A failed Attempt with Attempts left in the Round is replaced on the next tick, or after the pause.
        """
        if ticket.running is None:
            # Only Attempts wait out a pause, because Probes and Operations do not use the subscription.
            return not self._held() and self.attempts.start(ticket, node)
        ended = self.attempts.step(ticket, node, lambda: self._reconcile_ticket(ticket))
        if ended is None:
            return False
        return ended if ended.outcome is not None else True

    def _leave(self, ticket: TicketRuntime, node: Node, outcome: str, cause: dict[str, Any] | None) -> None:
        """Takes the route of `outcome` with `cause` for the next node.

        The route first starts the round count of the Stages it resets over.
        """
        resets = node.resets.get(outcome, ())
        if resets:
            self._append(Event.RoundsReset, {"ticket": ticket.id, "node": node.name, "stages": list(resets)})
        self._enter(ticket, self.pipeline.route(node, outcome), cause)

    def _enter(self, ticket: TicketRuntime, target: str, cause: dict[str, Any] | None) -> None:
        """Enters `target`, and starts a Round when it is a Stage or a Decision.

        `cause` is the artifact whose outcome led here.
        A Stage past its round limit is not entered and gives `exhausted`, which keeps the cause.
        """
        node = self.pipeline.nodes.get(target)
        if node is None or not node.HOLDS_WINDOW:
            self.window.release(ticket.id, ticket.node)
        payload: dict[str, Any] = {"ticket": ticket.id, "node": target, "at": self.ports.clock.now().isoformat()}
        if cause is not None:
            payload["cause"] = cause
        if isinstance(node, Stage | Decision):
            count = ticket.rounds_since_reset.get(target, 0) + 1
            limit = self.round_limits.get(target, node.max_rounds) if isinstance(node, Stage) else None
            if limit is not None and count > limit:
                to = self.pipeline.route(node, "exhausted")
                self._append(
                    Event.RoundsExhausted,
                    {"ticket": ticket.id, "node": target, "rounds": limit, "outcome": "exhausted", "to": to},
                )
                self._leave(ticket, node, "exhausted", cause)
                return
            payload["round"] = ticket.rounds.get(target, 0) + 1
            payload["rounds_since_reset"] = count
        self._append(Event.NodeEntered, payload)
        if target in TERMINAL_NODES and ticket.state == TicketState.MERGED:
            self._clean_up(ticket, ticket.merged_commit)
        if target == "abandoned" and ticket.state == TicketState.ABANDONED:
            self._abandon(ticket)

    def _apply_commands(self) -> bool:
        """Accepts or rejects each Command in the inbox of the Run, oldest first, and returns whether any came."""
        commands = self.journal.pending_commands(self.run_id)
        for command in commands:
            try:
                match parse(command):
                    case Decide() as answer if answer.ticket == RUN_LEVEL:
                        self._answer_run(command, answer)
                    case Decide() as answer:
                        ticket, decision, ruling = self._check_answer(answer)
                        self._carry_out_answer(command, ticket, decision, ruling)
                    case Cancel() as cancel:
                        self._cancel(command, cancel)
                    case OverrideCommand() as override:
                        self._override(command, override)
                    case Limits() as limits:
                        self._set_limits(command, limits)
            except (Rejected, BadCommand) as rejection:
                self._append(
                    Event.CommandRejected, {"command": command.id, "kind": command.kind, "reason": str(rejection)}
                )
        return bool(commands)

    def _ticket_of(self, ticket_id: str) -> TicketRuntime:
        ticket = next((t for t in self.tickets if t.id == ticket_id), None)
        if ticket is None:
            raise Rejected(f"no Ticket {ticket_id} in the Run")
        return ticket

    def _accept(self, command: CommandRow, ticket: str, result: str) -> None:
        self._append(
            Event.CommandAccepted, {"command": command.id, "kind": command.kind, "ticket": ticket, "result": result}
        )

    def _cancel(self, command: CommandRow, cancel: Cancel) -> None:
        """Abandons a Ticket that is queued or at work: its Attempt or Probe is stopped and its work is saved.

        A Ticket that depends on it never starts, since it only starts once all its blockers are merged.
        """
        ticket = self._ticket_of(cancel.ticket)
        if ticket.state not in (TicketState.QUEUED, TicketState.ACTIVE):
            raise Rejected(f"Ticket {ticket.id} is already {ticket.state}")
        queued = ticket.state == TicketState.QUEUED
        if not queued:
            self._stop_work(ticket, "cancelled", "the owner cancelled the Ticket")
        with self.journal.transaction():
            self._accept(command, ticket.id, f"Ticket {ticket.id} cancelled, abandoned")
            self._append(Event.TicketCancelled, {"ticket": ticket.id, "node": ticket.node})
        self.window.forget(ticket.id)
        if not queued:
            self._enter(ticket, "abandoned", ticket.cause)

    def _stop_work(self, ticket: TicketRuntime, cause: str, reason: str) -> None:
        """Stops the Attempt or Probe the Ticket has going and puts the Workspace back where it was committed.

        The Attempt is lost with `cause`, which spends none of the Stage's and no Round, and a merge that `sync`
        left in the Workspace is dropped.
        A Ticket that waits for the owner keeps the owner's edits in the Workspace.
        """
        assert ticket.workspace is not None
        running, ticket.running = ticket.running, None
        probe, ticket.probe = ticket.probe, None
        merging = ticket.conflict is not None
        if merging:
            self._append(Event.ConflictDropped, {"ticket": ticket.id})
        if probe is not None:
            probe.process.kill()
            self._append(Event.ProbeInterrupted, {"ticket": ticket.id, "node": ticket.node, "reason": reason})
        if running is not None:
            self.attempts.kill(running)
            key = {**attempt_key(ticket), "head_before": running.head_before}
            discarded = self.attempts.discard(ticket, running.head_before, running.request.attempt_dir)
            self._lose_attempt(ticket, key, cause, reason, [], discarded)
        elif probe is not None or merging:
            self.git.reset(ticket.workspace, ticket.branch, ignored=False)

    def _override(self, command: CommandRow, override: OverrideCommand) -> None:
        """Replaces the outcome of the Decision the Ticket is at, or else its last one, and takes the new route.

        What the Ticket was doing since that Decision is stopped, like the work of a cancelled Ticket.
        The new outcome gets an Artifact of its own next to the old one, which stays.
        """
        ticket = self._ticket_of(override.ticket)
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
        ruling = check_ruling(
            override.outcome,
            override.reasons,
            decision.outcomes,
            partial(self.pipeline.sends_back_to_work, decision),
        )
        outcome = ruling.outcome
        waited = self._take_owner_edits(ticket) if ticket.waiting is not None else None
        self._stop_work(ticket, "overridden", f"the owner overrode the Decision {decision.name}")
        replacement = Override(round)
        if previous is not None:
            earlier = sum(
                a["kind"] == "decision" and (a["node"], a["round"]) == (decision.name, round) for a in ticket.artifacts
            )
            replacement = Override(
                round,
                self.run_dir.decision(ticket.id, decision.name, round).with_name(f"override-{earlier}.json"),
                json.loads(Path(previous["path"]).read_text())["outcome"],
            )
        target = self.pipeline.route(decision, outcome)
        restart = ticket.waiting is not None and target not in TERMINAL_NODES
        with self.journal.transaction():
            self._accept(
                command, ticket.id, f"Ticket {ticket.id} overrode {decision.name}: {outcome}, goes to {target}"
            )
            _, artifact = self.decisions.record(ticket, decision, ruling, waited, replacement)
        if restart:
            self._restart_sandbox(ticket)
        self.window.forget(ticket.id)
        self._leave(ticket, decision, outcome, artifact)

    def _set_limits(self, command: CommandRow, limits: Limits) -> None:
        """Changes the Run's parallelism and the round limits of its Stages, which count from the next Round."""
        max_parallel, rounds = limits.max_parallel, limits.rounds
        if max_parallel is not None and max_parallel > 1:
            pipeline = self.pipeline
            if merges := merges_without_sync(pipeline.nodes, pipeline.start, pipeline.escalate_to):
                source, merge = merges[0]
                raise Rejected(
                    f"{max_parallel} Tickets at once need a sync before merge, but {source} reaches {merge} without one"
                )
        throttle = limits.throttle
        for window, shares in throttle.items():
            try:
                parse_window(window)
                if shares is not None:
                    parse_brake(shares)
            except values.Invalid as error:
                raise Rejected(f"{window}: {error}") from None
        for name in rounds:
            stage = self.pipeline.nodes.get(name)
            if not isinstance(stage, Stage) or stage.max_rounds is None:
                raise Rejected(f"{name} is no Stage with a round limit")
        with self.journal.transaction():
            changes = [] if max_parallel is None else [f"max_parallel {max_parallel}"]
            changes += [f"{name} rounds {limit}" for name, limit in rounds.items()]
            changes += [
                f"{window} brake lifted" if shares is None else f"{window} {_shares(shares)}"
                for window, shares in throttle.items()
            ]
            self._accept(command, "", f"Limits set: {', '.join(changes)}")
            self._append(Event.LimitsChanged, {"max_parallel": max_parallel, "rounds": rounds, "throttle": throttle})

    def _check_answer(self, answer: Decide) -> tuple[TicketRuntime, Decision, deciders.Ruling]:
        """Returns the Ticket, Decision and Ruling a `decide` Command asks for, or raises Rejected with the reason."""
        ticket = self._ticket_of(answer.ticket)
        asking = Asking(f"Ticket {ticket.id}", ticket.waiting, ticket.answered)
        check_asked(asking, answer.node, answer.round)
        assert ticket.waiting is not None and ticket.workspace is not None
        if not self.git.is_ancestor(ticket.waiting_head, self.git.head(ticket.workspace, ticket.branch)):
            raise Rejected("the history of the Workspace was rewritten while the Decision waited")
        decision = self.pipeline.nodes[ticket.waiting[0]]
        assert isinstance(decision, Decision)
        ruling = check_ruling(
            answer.outcome,
            answer.reasons,
            decision.outcomes,
            partial(self.pipeline.sends_back_to_work, decision),
        )
        return ticket, decision, ruling

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
                Event.CommandAccepted,
                {"command": command.id, "kind": command.kind, "ticket": ticket.id, "result": result},
            )
            outcome, artifact = self.decisions.record(ticket, decision, ruling, waited)
        if target not in TERMINAL_NODES:
            self._restart_sandbox(ticket)
        self._leave(ticket, decision, outcome, artifact)

    def _restart_sandbox(self, ticket: TicketRuntime) -> None:
        """Starts the Sandbox of a Ticket that waited for the owner, which the answer or the override ends."""
        try:
            self.ports.sandbox.start(self._sandbox_name(ticket))
        except SandboxError as error:
            self._append(Event.SandboxStartFailed, {"ticket": ticket.id, "reason": str(error)})

    def _take_owner_edits(self, ticket: TicketRuntime) -> dict[str, str]:
        """Commits what the owner left uncommitted in the Workspace while the Decision waited, as the human.

        Returns the `head_before` and `head_after` of the waiting, whose range holds the owner's commits.
        """
        assert ticket.workspace is not None
        trailers = {"Factory-Run": self.run_id, "Factory-Ticket": ticket.id, "Factory-Actor": "human"}
        self.git.commit_all(ticket.workspace, ticket.branch, f"Owner edits to {ticket.id}", trailers)
        waited = {"head_before": ticket.waiting_head, "head_after": self.git.head(ticket.workspace, ticket.branch)}
        if waited["head_after"] != waited["head_before"]:
            self._append(Event.OwnerEditsCommitted, {"ticket": ticket.id, **waited})
        return waited

    def _start_probe(self, ticket: TicketRuntime, gate: Gate) -> None:
        """Starts the Gate's Probe in the Ticket's Sandbox on the committed state of the Workspace.

        A Probe the Sandbox cannot start is lost.
        """
        assert ticket.workspace is not None
        number = ticket.probes.get(gate.name, 0) + 1
        probe_dir = self.run_dir.probe(ticket.id, gate.name, number)
        key = {"ticket": ticket.id, "node": gate.name, "attempt": ticket.gate_attempt + 1, "probe": number}
        self.git.reset(ticket.workspace, ticket.branch, ignored=gate.clean)
        sandbox = self._sandbox_name(ticket)
        self.attempts.reap(ticket, sandbox)
        self._append(
            Event.ProbeStarted,
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
            process = self.ports.sandbox.exec(sandbox, probe.argv, stdout=probe_dir.log, env=env)
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
        self._append(Event.ProbeLost, {**key, "reason": reason})
        self.git.reset(ticket.workspace, ticket.branch, ignored=False)
        if self.attempts.sandbox_gone_too_often(ticket, "probe_lost", reason):
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

    def _evaluate(self, ticket: TicketRuntime, gate: Gate, probe: gates.ProbeResult) -> tuple[str, dict[str, Any]]:
        """Writes the verdict on the finished Probe and returns the Gate's outcome with the verdict as its artifact."""
        assert ticket.probe is not None
        running, ticket.probe = ticket.probe, None
        return self._finish_gate(ticket, gate, probe, running.dir.verdict, running.key)

    def _evaluate_borrowed(self, ticket: TicketRuntime, gate: Gate, source: str) -> tuple[str, dict[str, Any]]:
        """Evaluates a Gate with `from` on the latest Probe of its source Gate, and runs no Probe of its own.

        The source Gate judged the Probe's exit code and timeout, so this verdict reads only its reports.
        Config makes every path pass through the source, so the Workspace is as the source's Probe left it.
        """
        assert source in ticket.probe_results, "Config proves that every path to a Gate passes through its from"
        number, probe = ticket.probe_results[source]
        key = {
            "ticket": ticket.id,
            "node": gate.name,
            "attempt": ticket.gate_attempt + 1,
            "from": source,
            "probe": number,
        }
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
            "duration": ticket.seconds_in_node(self.ports.clock.now()),
            "probe_duration": verdict.get("duration"),
            "failed_lines": (verdict.get("summary") or {}).get("matched_count"),
        }
        self._append(Event.GateFinished, payload)
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
            artifact = self._conflict_artifact(ticket, operation, tip, head, error.files)
            self._operation_finished(
                ticket, operation, tip, "conflict", head, reason=str(error), artifact=artifact, files=error.files
            )
            return "conflict", artifact
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
        return {"kind": "operation", "node": operation.name, "path": str(path)}

    def _merge(self, ticket: TicketRuntime, operation: Operation, tip: str) -> tuple[str, dict[str, Any] | None]:
        """Fast-forwards the Integration branch to the Ticket branch's head, and gives `merged`."""
        head = self.git.resolve(ticket.branch)
        self.git.fast_forward(self.integration, head, tip)
        self._operation_finished(ticket, operation, tip, "merged", head, **self.git.size(tip, head))
        return "merged", None

    def _operation_finished(
        self, ticket: TicketRuntime, operation: Operation, before: str, outcome: str, after: str, **extra: Any
    ) -> None:
        payload = {"ticket": ticket.id, "node": operation.name, "kind": operation.kind, "before": before}
        duration = ticket.seconds_in_node(self.ports.clock.now())
        self._append(
            Event.OperationFinished, {**payload, "outcome": outcome, "after": after, "duration": duration, **extra}
        )

    def _abandon(self, ticket: TicketRuntime) -> None:
        """Saves the head of an abandoned Ticket's branch under its ref, then cleans the Ticket up.

        A head that cannot be saved is journaled with its reason and keeps the Sandbox, Workspace and branch,
        because the branch would be the only place left to find the work.
        A Ticket that never got a Workspace has nothing to save or clean up, and one whose branch still points at its
        base holds no work, so it gets no ref.
        """
        if ticket.workspace is None:
            self._append(Event.AbandonedWithoutWorkspace, {"ticket": ticket.id})
            return
        ref = abandoned_ref(self.run_id, ticket.id)
        try:
            head = self.git.resolve(ticket.branch)
            if head == ticket.base:
                self._append(Event.AbandonedWithoutWork, {"ticket": ticket.id, "commit": head})
                self._clean_up(ticket, head)
                return
            self.git.keep(ref, head)
        except GitError as error:
            self._cleanup_failed(ticket, "ref", error)
            return
        self._append(Event.AbandonedBranchSaved, {"ticket": ticket.id, "ref": ref, "commit": head})
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
        events = CleanupEvents(
            removed=lambda: self._append(Event.SandboxRemoved, {"ticket": ticket.id, "sandbox": sandbox}),
            left_behind=lambda reason: self._append(
                Event.SandboxLeftBehind, {"ticket": ticket.id, "sandbox": sandbox, "reason": reason}
            ),
            workspace_removed=lambda: self._append(
                Event.WorkspaceRemoved, {"ticket": ticket.id, "workspace": str(ticket.workspace)}
            ),
            branch_deleted=lambda: self._append(
                Event.TicketBranchDeleted, {"ticket": ticket.id, "branch": ticket.branch, "commit": commit}
            ),
            failed=lambda step, error: self._cleanup_failed(ticket, step, error),
        )
        self.attempts.clean_up(sandbox, ticket.workspace, ticket.branch, commit, events)

    def _cleanup_failed(self, ticket: TicketRuntime, step: str, error: Exception) -> None:
        self._append(Event.CleanupFailed, {"ticket": ticket.id, "step": step, "reason": str(error)})

    def _sandbox_name(self, ticket: TicketRuntime) -> str:
        return sandbox_name(self.run_id, ticket.id)

    def _append(self, type: Event, payload: dict[str, Any]) -> None:
        """Journals the event and applies it to the Run state, the only way the state changes."""
        self.journal.append(self.run_id, type, payload)
        apply(self.pipeline, self.memory, type, payload)
