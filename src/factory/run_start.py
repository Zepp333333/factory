"""Run start is the first phase of a Run: where its Integration branch begins, the frozen Library, and the Run's state.

A new Run begins at the commit `resolve_base` finds, and a Run that resumes is rebuilt from its events.
Reconciliation, which keeps the world of the active Tickets as the journal says it is, is part of this phase
because it mends what a dead Orchestrator left and then goes on every few seconds.
"""

import secrets
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from factory.artifacts import RunDir
from factory.attempt import Attempts, Ports, attempt_key
from factory.clock import Clock
from factory.config import Project
from factory.custody import RunCustody
from factory.events import Event
from factory.fingerprint import roles
from factory.git import GitError, HostGit
from factory.journal import Journal
from factory.landing import landing_remote
from factory.live import LiveTicket
from factory.merge_window import MergeWindow
from factory.names import integration_branch, sandbox_name
from factory.pipeline import Pipeline
from factory.recovery import replay
from factory.run_state import RunMemory, apply
from factory.sandbox import Mount, SandboxError
from factory.states import TicketState
from factory.tracker import PullRequestFolder, PullRequests, SpecSnapshot, Tracker

# Reconciliation checks the world of the running Tickets this often, in seconds.
RECONCILE_SECONDS = 5


def new_run_id(clock: Clock) -> str:
    return f"{clock.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


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


def freeze_library(project: Project, home: Path, run_id: str) -> None:
    """Freezes the Library entries the Pipeline's Roles use into the Run's directory."""
    assert project.library is not None
    project.library.freeze(roles(project.pipeline), project.pipeline.path, RunDir(home / "runs" / run_id).library)


class Scope(Protocol):
    """A Scope is the Run that starts or resumes: what it learns of its Spec and branch, and what its events rebuild."""

    run_id: str
    run_dir: RunDir
    integration: str
    snapshot: SpecSnapshot
    tracker: Tracker
    base_commit: str
    memory: RunMemory
    started: bool
    pull_request: str

    @property
    def tickets(self) -> list[LiveTicket]: ...


class RunStart:
    """RunStart starts a Run on a Spec snapshot, takes up an orphaned Run from its events and reconciles its world.

    `append` journals an event and applies it to the Run state, `mounts` lists what a Ticket's Sandbox sees,
    and `lose_attempt` loses an Attempt of a Ticket.
    """

    def __init__(
        self,
        journal: Journal,
        append: Callable[[Event, dict[str, Any]], None],
        project: Project,
        pipeline: Pipeline,
        ports: Ports,
        home: Path,
        git: HostGit,
        attempts: Attempts,
        window: MergeWindow,
        custody: RunCustody,
        mounts: Callable[[LiveTicket], list[Mount]],
        lose_attempt: Callable[[LiveTicket, dict[str, Any], str, str, list[str], dict[str, Any]], None],
        scope: Scope,
    ) -> None:
        self.journal = journal
        self.append = append
        self.project = project
        self.pipeline = pipeline
        self.ports = ports
        self.home = home
        self.git = git
        self.attempts = attempts
        self.window = window
        self.custody = custody
        self.mounts = mounts
        self.lose_attempt = lose_attempt
        self.scope = scope
        self.reconciled: datetime | None = None

    def begin(self, run_id: str, snapshot: SpecSnapshot, tracker: Tracker, base_commit: str) -> None:
        """Starts the Run on the snapshot the Tracker gave, and lands it through the PullRequests the Run was given.

        The Integration branch begins at `base_commit`, which `resolve_base` found.
        """
        self.scope.run_id = run_id
        self.scope.integration = integration_branch(run_id)
        self.scope.run_dir = RunDir(self.home / "runs" / run_id)
        self.scope.snapshot = snapshot
        self.scope.tracker = tracker
        self.scope.base_commit = base_commit
        self.ports.executor.assemble(self.scope.run_dir.skills, self.scope.run_dir.kit)

        self.append(
            Event.RunStarted,
            {
                "spec_ref": snapshot.ref,
                "project": str(self.project.path),
                "pipeline": str(self.pipeline.path),
                "run_dir": str(self.scope.run_dir.root),
                "integration_branch": self.scope.integration,
                "max_parallel": self.scope.memory.max_parallel,
                "sandbox_egress": list(self.ports.sandbox.needs.egress),
                "sandbox_env": sorted(self.ports.sandbox.needs.env),
                **self.attempts.variant.run,
            },
        )
        self.scope.started = True
        self.custody.begin()
        self.append(Event.SpecSnapshotted, {"snapshot": snapshot.to_json()})
        self.append(Event.SpecRendered, self.scope.run_dir.render(snapshot))
        self.git.create_branch(self.scope.integration, base_commit)
        self.append(
            Event.IntegrationBranchCreated,
            {"branch": self.scope.integration, "base_ref": self.project.base, "commit": base_commit},
        )
        self.scope.memory.tickets = [LiveTicket(id=t.id, blocked_by=t.blocked_by) for t in snapshot.tickets]

    def resume(self, run_id: str, tracker: Tracker) -> None:
        """Takes up an orphaned Run from its events and puts its Tickets back where the journal says they were.

        An agent that outlived the dead Orchestrator is stopped and never adopted, so the Sandbox is stopped
        and the Attempt is lost; the Workspace goes back to the commit the Stage began at, and a new Attempt follows.
        A Probe in flight starts again.
        A Decision that waits stays waiting, and its Command in the inbox is applied on the first tick.
        """
        state = self.reclaim(run_id, tracker)
        for ticket in self.scope.tickets:
            if ticket.state == TicketState.ACTIVE:
                self._take_over(ticket, state.in_flight.get(ticket.id), ticket.id in state.probing)

    def reclaim(self, run_id: str, tracker: Tracker) -> RunMemory:
        """Takes an orphaned Run back from its events and starts beating for it, and returns what the events say."""
        state = self._reload(run_id, tracker)
        payload = {"in_flight": sorted(state.in_flight), "probing": sorted(state.probing)}
        self.custody.reclaim(run_id, payload)
        self.scope.started = True
        apply(self.pipeline, self.scope.memory, Event.RunReclaimed, payload)
        return state

    def _reload(self, run_id: str, tracker: Tracker) -> RunMemory:
        """Rebuilds the Run's Tickets and Merge window from its events and returns the rest of what they say."""
        events = self.journal.events(run_id)
        self.scope.run_id = run_id
        self.scope.integration = integration_branch(run_id)
        self.scope.run_dir = RunDir(self.home / "runs" / run_id)
        self.ports.executor.assemble(self.scope.run_dir.skills, self.scope.run_dir.kit)
        self.scope.tracker = tracker
        self.scope.snapshot = SpecSnapshot.from_json(
            next(e.payload["snapshot"] for e in events if e.type == Event.SpecSnapshotted)
        )
        self.scope.base_commit = next(e.payload["commit"] for e in events if e.type == Event.IntegrationBranchCreated)
        tickets = [LiveTicket(id=t.id, blocked_by=t.blocked_by) for t in self.scope.snapshot.tickets]
        self.scope.memory = replay(self.pipeline, tickets, events, self.project.max_parallel, self.project.throttle)
        self.window.restore(self.scope.memory.holder, self.scope.memory.queue)
        self.scope.pull_request = self.scope.memory.pull_request
        return self.scope.memory

    def _take_over(self, ticket: LiveTicket, attempt: dict[str, Any] | None, probing: bool) -> None:
        """Brings one active Ticket of a resumed Run back: stops what its dead Orchestrator left running in its Sandbox,
        loses the Attempt in flight and mends what the world lacks.
        """
        assert ticket.workspace is not None
        self.stop_leftover(ticket, attempt, probing)
        mended = self._mend_world(ticket, attempt["head_before"] if attempt else self._known_head(ticket))
        if probing:
            self.git.reset(ticket.workspace, ticket.branch, ignored=False)
            self.append(
                Event.ProbeInterrupted, {"ticket": ticket.id, "node": ticket.node, "reason": "the Run was orphaned"}
            )
        if attempt is not None:
            attempt_dir = Path(attempt["stream"]).parent
            discarded = self.attempts.discard(ticket, attempt["head_before"], attempt_dir)
            self.lose_attempt(ticket, attempt, "orchestrator_lost", "the Run's process died", mended, discarded)

    def stop_leftover(self, ticket: LiveTicket, attempt: dict[str, Any] | None, probing: bool) -> None:
        """Stops the agent or Probe a dead Orchestrator left running in the Ticket's Sandbox."""
        assert ticket.workspace is not None
        try:
            name = sandbox_name(self.scope.run_id, ticket.id)
            if (attempt or probing) and self.ports.sandbox.attach(name, ticket.workspace):
                self.ports.sandbox.stop(name)
        except SandboxError as error:
            self.append(Event.SandboxStopFailed, {"ticket": ticket.id, "reason": str(error)})

    def reconcile(self) -> bool:
        """Checks the world of the active Tickets against the journal now and then, and returns whether it found drift.

        A Ticket whose Sandbox, Workspace or branch is gone loses its running Attempt, and the world is made again.
        """
        now = self.ports.clock.now()
        if self.reconciled is not None and (now - self.reconciled).total_seconds() < RECONCILE_SECONDS:
            return False
        self.reconciled = now
        drift = False
        for ticket in self.scope.tickets:
            if ticket.state == TicketState.ACTIVE and ticket.waiting is None:
                drift |= self.reconcile_ticket(ticket)
        return drift

    def reconcile_ticket(self, ticket: LiveTicket) -> bool:
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
            self.append(Event.WorldMended, {"ticket": ticket.id, "mended": mended})
            return True
        key = {**attempt_key(ticket), "head_before": running.head_before}
        cause = {"sandbox": "sandbox_lost", "workspace": "workspace_lost", "branch": "branch_lost"}[missing[-1]]
        reason = f"the {', '.join(missing)} of the Ticket was gone"
        discarded = self.attempts.discard(ticket, running.head_before, running.request.attempt_dir)
        self.lose_attempt(ticket, key, cause, reason, mended, discarded)
        return True

    def _missing(self, ticket: LiveTicket) -> list[str]:
        """Names what the Ticket lacks of its branch, Workspace and Sandbox, the one every other depends on last."""
        assert ticket.workspace is not None
        missing = []
        if not self._branch_exists(ticket.branch):
            missing.append("branch")
        if not self.ports.workbench.has_workspace(self.project.repo, ticket.workspace):
            missing.append("workspace")
        if not self.ports.sandbox.attach(sandbox_name(self.scope.run_id, ticket.id), ticket.workspace):
            missing.append("sandbox")
        return missing

    def _mend_world(self, ticket: LiveTicket, head: str) -> list[str]:
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
            self.ports.sandbox.create(sandbox_name(self.scope.run_id, ticket.id), ticket.workspace, self.mounts(ticket))
        return missing

    def _known_head(self, ticket: LiveTicket) -> str:
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
