"""Run start is the first phase of a Run: where its Integration branch begins, the frozen Library, and the Run's state.

A new Run begins at the commit `resolve_base` finds, and a Run that resumes or finishes is rebuilt from its events.
A Run that finishes gives up the Tickets it did not merge.
Reconciliation, which keeps the world of the active Tickets as the journal says it is, is part of this phase
because it mends what a dead Orchestrator left and then goes on every few seconds.
"""

import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from factory.config import Project
from factory.core.clock import Clock
from factory.core.events import Event, applies, measures, nothing, times
from factory.core.git import GitError, HostGit
from factory.core.states import TicketState
from factory.journal.artifacts import RunDir
from factory.journal.custody import RunCustody
from factory.journal.metrics_memory import MetricsMemory, blockers_of
from factory.journal.run_state import RunMemory, apply
from factory.journal.timeline_memory import TimelineMemory
from factory.pipeline.fingerprint import roles
from factory.ports.run_ports import Ports
from factory.ports.sandbox import SandboxError
from factory.ports.tracker import PullRequests
from factory.run.attempt import Attempts
from factory.run.landing import landing_remote
from factory.run.live import LiveTicket, attempt_key
from factory.run.merge_window import MergeWindow
from factory.run.moves import Moves
from factory.run.run_context import Append, RunContext

# Reconciliation checks the world of the running Tickets this often, in seconds.
RECONCILE_SECONDS = 5


def new_run_id(clock: Clock) -> str:
    return f"{clock.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def resolve_base(git: HostGit, base: str, pull_requests: PullRequests) -> str | None:
    """Fetches `base` from the remote the Run pushes to and returns the commit the Integration branch starts at.

    It returns None when the project has no remote the PR lands through, and the Run then starts from the local `base`.
    It raises GitError when the fetch fails or when the local `base` holds commits the remote lacks,
    so local work is never dropped silently.
    """
    if not pull_requests.pushes:
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


def frozen_library(home: Path, run_id: str) -> Path:
    """Returns the directory of the Run's own Layer, which holds the Library entries the Run froze."""
    return RunDir(home / "runs" / run_id).library


def freeze_library(project: Project, home: Path, run_id: str) -> None:
    """Freezes the Library entries the Pipeline's Roles use into the Run's directory."""
    assert project.library is not None
    project.library.freeze(roles(project.pipeline), project.pipeline.path, frozen_library(home, run_id))


class RunStart:
    """RunStart starts a Run on a Spec snapshot, takes up or gives up an orphaned Run from its events,
    and reconciles its world.
    """

    def __init__(
        self,
        context: RunContext,
        append: Append,
        project: Project,
        ports: Ports,
        git: HostGit,
        attempts: Attempts,
        window: MergeWindow,
        custody: RunCustody,
        moves: Moves,
    ) -> None:
        self.append = append
        self.project = project
        self.pipeline = project.pipeline
        self.ports = ports
        self.git = git
        self.attempts = attempts
        self.window = window
        self.custody = custody
        self.lose_attempt = moves.lose_attempt
        self.operations = moves.operations
        self.context = context
        self.reconciled: datetime | None = None

    def begin(self) -> None:
        """Starts the Run on the snapshot the Tracker gave, and lands it through the PullRequests the Run was given.

        The Integration branch begins at the context's `base_commit`, which `resolve_base` found.
        """
        context = self.context
        self.ports.executor.assemble(self.ports.kit, context.run_dir.skills, context.run_dir.kit)
        self.append(
            Event.RunStarted,
            {
                "spec_ref": context.snapshot.ref,
                "project": str(self.project.path),
                "pipeline": str(self.pipeline.path),
                "run_dir": str(context.run_dir.root),
                "integration_branch": context.integration,
                "max_parallel": context.memory.max_parallel,
                "sandbox_egress": list(self.ports.sandbox.needs.egress),
                "sandbox_env": sorted(self.ports.sandbox.needs.env),
                "settings": self.project.settings,
                "settings_unlike_file": list(self.project.unlike_file),
                **self.attempts.variant.run,
            },
        )
        self.custody.begin()
        self.append(Event.SpecSnapshotted, {"snapshot": context.snapshot.to_json()})
        self.append(Event.SpecRendered, context.run_dir.render(context.snapshot))
        self.git.create_branch(context.integration, context.base_commit)
        self.append(
            Event.IntegrationBranchCreated,
            {"branch": context.integration, "base_ref": self.project.base, "commit": context.base_commit},
        )

    def resume(self) -> None:
        """Takes up an orphaned Run from its events and puts its Tickets back where the journal says they were.

        An agent that outlived the dead Orchestrator is stopped and never adopted, so the Sandbox is stopped
        and the Attempt is lost; the Workspace goes back to the commit the Stage began at, and a new Attempt follows.
        A Probe in flight starts again.
        A Decision that waits stays waiting, and its Command in the inbox is applied on the first tick.
        """
        state = self.reclaim()
        for ticket in self.context.tickets:
            if ticket.state == TicketState.ACTIVE:
                self._take_over(ticket, state.in_flight.get(ticket.id), ticket.id in state.probing)

    def reclaim(self) -> RunMemory:
        """Takes an orphaned Run, which its context rebuilt from its events, back and starts beating for it.

        It returns the state the events tell.
        """
        memory = self.context.memory
        self.ports.executor.assemble(self.ports.kit, self.context.run_dir.skills, self.context.run_dir.kit)
        self.window.restore(memory.holder, memory.queue)
        payload = {"in_flight": sorted(memory.in_flight), "probing": sorted(memory.probing)}
        self.custody.reclaim(self.context.run_id, payload)
        apply(self.pipeline, memory, Event.RunReclaimed, payload)
        return memory

    def give_up(self, ticket: LiveTicket, attempt: dict[str, Any] | None, probing: bool) -> None:
        """Abandons a Ticket the owner finished the Run without.

        The agent its dead Orchestrator left is stopped first, and what it left uncommitted in the Workspace is
        committed to the Ticket branch, so the ref `Operations.abandon` saves holds it.
        """
        was_active = ticket.state == TicketState.ACTIVE
        if was_active:
            self._save_unsaved(ticket, attempt, probing)
        self.append(
            Event.NodeEntered,
            {
                "ticket": ticket.id,
                "node": "abandoned",
                "at": self.ports.clock.now().isoformat(),
                "reason": "the owner finished the Run",
            },
        )
        self.operations.abandon(ticket)

    def _save_unsaved(self, ticket: LiveTicket, attempt: dict[str, Any] | None, probing: bool) -> None:
        """Stops the agent a dead Orchestrator left for a Ticket and commits what it left in the Workspace."""
        assert ticket.workspace is not None
        self.stop_leftover(ticket, attempt, probing)
        trailers = {"Factory-Run": self.context.run_id, "Factory-Ticket": ticket.id, "Factory-Actor": "finish"}
        try:
            commit = self.git.commit_all(ticket.workspace, ticket.branch, f"Unsaved work of {ticket.id}", trailers)
        except (GitError, OSError) as error:
            self.operations.cleanup_failed(ticket, "unsaved", error)
            return
        if commit is not None:
            self.append(Event.UnsavedWorkCommitted, {"ticket": ticket.id, "commit": commit})

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
            name = self.context.sandbox(ticket.id)
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
        for ticket in self.context.tickets:
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
        if not self.ports.sandbox.attach(self.context.sandbox(ticket.id), ticket.workspace):
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
            mounts = self.attempts.isolation.mounts(ticket, self.context.run_dir)
            self.ports.sandbox.create(self.context.sandbox(ticket.id), ticket.workspace, mounts)
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


# What the events of a Run's start tell the readers of the Journal.

applies(
    Event.RunStarted,
    Event.SpecSnapshotted,
    Event.SpecRendered,
    Event.IntegrationBranchCreated,
    Event.WorldMended,
    Event.UnsavedWorkCommitted,
)(nothing)


@times(Event.RunStarted)
def _time_run_started(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    memory.started = memory.at
    memory.line("run", "started", payload["spec_ref"])


@measures(Event.RunStarted)
def _measure_run_started(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if memory.run is None:
        memory.run, memory.begin, memory.cap = payload, memory.at, payload.get("max_parallel")


@measures(Event.SpecSnapshotted)
def _measure_spec_snapshotted(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if memory.blockers is None:
        memory.blockers = blockers_of(payload["snapshot"])
