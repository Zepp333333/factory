"""The Orchestrator is the deterministic loop that drives one Run and is the only writer of its events."""

import json
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from factory import deciders
from factory.artifacts import RunDir
from factory.attempt import (
    RATE_LIMIT_PAUSE as RATE_LIMIT_PAUSE,
)
from factory.attempt import (
    Attempts,
    Ended,
    Ports,
    attempt_key,
)
from factory.ci_wait import CI_NOTES, CiWaiter, ci_payload
from factory.commands import RUN_LEVEL, BadCommand, Cancel, Decide, Limits, parse
from factory.commands import Override as OverrideCommand
from factory.config import Project
from factory.custody import HEARTBEAT_SECONDS, RunCustody
from factory.decision import Asking, Decisions, Override, Rejected, check_asked, check_limits, check_ruling
from factory.events import Event
from factory.git import GitError, HostGit
from factory.journal import CommandRow, Journal
from factory.landing import Lander
from factory.live import LiveTicket
from factory.merge_window import MergeWindow
from factory.names import sandbox_name, ticket_branch
from factory.operations import Operations
from factory.pipeline import Decision, Gate, Node, Operation, Stage
from factory.probes import Probes
from factory.projection import Projection
from factory.run_context import RunContext
from factory.run_decision import OWNER_ABORTED, RunDecisions
from factory.run_start import RunStart
from factory.run_state import (
    RunMemory,
    apply,
)
from factory.run_view import RunView
from factory.sandbox import Mount, SandboxLost
from factory.sleep_block import SleepBlock
from factory.states import TERMINAL_NODES, RunState, TicketState
from factory.throttle import Braked, Throttle
from factory.tracker import (
    PullRequestFolder,
    PullRequests,
)
from factory.workbench import WorkbenchError

TICK_SECONDS = 0.2

# A delivery to the Tracker that failed is tried again after this many seconds.
TRACKER_RETRY_SECONDS = 30


def _shares(shares: dict[str, float]) -> str:
    return ", ".join(f"{name} {share}" for name, share in shares.items())


class Orchestrator:
    """The Orchestrator drives the Run of its context.

    Only `_append` changes the Run's state, and the parts that do the work of a phase read the same context.
    """

    def __init__(
        self,
        journal: Journal,
        project: Project,
        ports: Ports,
        home: Path,
        view: RunView,
        slots: int,
        pull_requests: PullRequests | PullRequestFolder,
        context: RunContext,
    ) -> None:
        self.journal = journal
        self.pull_requests = pull_requests
        self.project = project
        self.pipeline = project.pipeline
        self.ports = ports
        self.home = home
        self.view = view
        self.slots = slots
        self.context = context
        self.git = HostGit(project.repo)
        self.throttle = Throttle()
        self.by_hand: list[list[str]] = []
        self.window = MergeWindow(self._append)
        self.run_decisions = RunDecisions(journal, self._append, view, context)
        self.attempts = Attempts(
            journal,
            self._append,
            project,
            self.pipeline,
            ports,
            self.git,
            context,
            slots,
            TICK_SECONDS,
            self._beat_and_show,
            self.run_decisions.ask,
        )
        self.decisions = Decisions(self._append, self.pipeline, ports, self.git, context, self.view.ring)
        self.custody = RunCustody(journal, ports.clock)
        self.probes = Probes(self._append, ports, self.git, self.attempts, self._leave, context)
        self.operations = Operations(self._append, ports, self.git, self.attempts, context)
        self.lander = Lander(
            self._append, project, self.pipeline, ports, home, self.git, self.attempts, pull_requests, context
        )
        self.ci_wait = CiWaiter(
            journal,
            self._append,
            project,
            ports,
            view,
            pull_requests,
            self._beat,
            self._apply_commands,
            TICK_SECONDS,
            context,
        )
        self.run_start = RunStart(
            journal,
            self._append,
            project,
            self.pipeline,
            ports,
            home,
            self.git,
            self.attempts,
            self.window,
            self.custody,
            self._mounts,
            self._lose_attempt,
            context,
        )
        self.retry_delivery_at: datetime | None = None

    @property
    def run_id(self) -> str:
        return self.context.run_id

    @property
    def run_dir(self) -> RunDir:
        return self.context.run_dir

    @property
    def memory(self) -> RunMemory:
        return self.context.memory

    @property
    def tickets(self) -> list[LiveTicket]:
        return self.context.tickets

    @property
    def started(self) -> bool:
        """Tells whether this process began the Run or took it up, so that it has a Run to end."""
        return self.custody.began

    @property
    def pull_request(self) -> str:
        return self.memory.pull_request

    @property
    def ci_note(self) -> str:
        return CI_NOTES.get(self.memory.ci_ended, "")

    @property
    def paused_until(self) -> datetime | None:
        return self.memory.paused_until

    @property
    def braked(self) -> Braked | None:
        return self.memory.braked

    @property
    def run_waiting(self) -> tuple[str, int] | None:
        return self.memory.run_waiting

    @property
    def max_parallel(self) -> int:
        return self.memory.max_parallel

    @property
    def round_limits(self) -> dict[str, int]:
        return self.memory.round_limits

    def start(self) -> None:
        """Starts the Run on the snapshot the Tracker gave, and lands it through the PullRequests the Run was given."""
        self.run_start.begin()

    def resume(self) -> None:
        """Takes up an orphaned Run from its events and puts its Tickets back where the journal says they were."""
        self.run_start.resume()

    def journal_sleep_block(self, block: SleepBlock) -> None:
        """Journals that the Run keeps the computer awake, or why it cannot."""
        if block.reason is None:
            self._append(Event.SleepBlocked, {"argv": block.argv})
            return
        self._append(Event.SleepBlockUnavailable, {"reason": block.reason})

    def finish(self) -> RunState:
        """Ends an orphaned Run as `partial` with what it merged, and lands that.

        A Run orphaned in phase `ci` has landed already: it ends without waiting for the CI, which the journal records
        as unchecked, and its state is that of any Run whose Tickets all merged.
        No session of an unfinished Ticket comes back: each Ticket that did not merge is abandoned,
        with its unsaved work kept under its ref, and the Run lands like any Run that ends `partial`.
        """
        state = self.run_start.reclaim()
        for ticket in self.tickets:
            if ticket.state in (TicketState.QUEUED, TicketState.ACTIVE):
                self._give_up(ticket, state.in_flight.get(ticket.id), ticket.id in state.probing)
        self._deliver(last=True)
        self.by_hand = self.lander.land()
        if self.memory.ci is not None:
            # The PR is open and the owner ends the Run without waiting for its CI.
            self._append(Event.CiFinished, ci_payload("unchecked", self.memory.ci.commit, []))
            self._finish(self._final_state())
            return self._final_state()
        self._finish(RunState.PARTIAL)
        return RunState.PARTIAL

    def _give_up(self, ticket: LiveTicket, attempt: dict[str, Any] | None, probing: bool) -> None:
        """Abandons a Ticket the owner finished the Run without.

        The agent its dead Orchestrator left is stopped first, and what it left uncommitted in the Workspace is
        committed to the Ticket branch, so the ref `Operations.abandon` saves holds it.
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
        self.operations.abandon(ticket)

    def _save_unsaved(self, ticket: LiveTicket, attempt: dict[str, Any] | None, probing: bool) -> None:
        """Stops the agent a dead Orchestrator left for a Ticket and commits what it left in the Workspace."""
        assert ticket.workspace is not None
        self.run_start.stop_leftover(ticket, attempt, probing)
        trailers = {"Factory-Run": self.run_id, "Factory-Ticket": ticket.id, "Factory-Actor": "finish"}
        try:
            commit = self.git.commit_all(ticket.workspace, ticket.branch, f"Unsaved work of {ticket.id}", trailers)
        except (GitError, OSError) as error:
            self.operations.cleanup_failed(ticket, "unsaved", error)
            return
        if commit is not None:
            self._append(Event.UnsavedWorkCommitted, {"ticket": ticket.id, "commit": commit})

    def _lose_attempt(
        self,
        ticket: LiveTicket,
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

    def _conclude(self, ticket: LiveTicket, ended: Ended) -> None:
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

    def run(self) -> RunState:
        """Ticks until no Ticket can move and returns the Run's final state."""
        self.view.show(self.journal, self.run_id)
        while True:
            moved = self._tick()
            if self.memory.aborted:
                self.abort(OWNER_ABORTED)
                return RunState.ABORTED
            if moved:
                self.view.show(self.journal, self.run_id)
                continue
            self.view.tick(self.journal, self.run_id)
            if (
                self.attempts.starved
                or self.run_waiting
                or self.operations.waiting
                or any(t.busy or t.waiting for t in self.tickets)
            ):
                self.ports.clock.sleep(TICK_SECONDS)
            elif waits := [t for t in (self.paused_until, self.braked and self.braked.until) if t]:
                until_reset = (min(waits) - self.ports.clock.now()).total_seconds()
                self.ports.clock.sleep(min(max(until_reset, TICK_SECONDS), HEARTBEAT_SECONDS))
            else:
                break
        self._deliver(last=True)
        state = self._final_state()
        self.by_hand = self.lander.land()
        self.ci_wait.wait()
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
        moved |= self.run_start.reconcile()
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
        failure = Projection(self.journal, self.run_id, self.context.tracker, self._append).deliver()
        if failure is None:
            self.retry_delivery_at = None
            return
        self.retry_delivery_at = now + timedelta(seconds=TRACKER_RETRY_SECONDS)
        self._append(Event.TrackerDeliveryFailed, failure)

    def _beat(self) -> None:
        """Beats for the Run, which raises RunTakenOver when another process took the Run over."""
        self.custody.beat(self.run_id)

    def _beat_and_show(self) -> None:
        """Beats for the Run and shows the tree, so the Describer's phase line appears and its time moves."""
        self._beat()
        self.view.show(self.journal, self.run_id)

    def _mark_stage(self, ticket: LiveTicket) -> None:
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
            or self.memory.aborted
        )

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

    def _claim(self, ticket: LiveTicket) -> bool:
        """Claims the Ticket and starts its first Attempt, and returns False when no slot is free.

        The slot, the claim and the record of the Attempt are one transaction of the Journal, so no other Run
        can take the slot in between.
        The Workspace starts from the Integration branch's tip as it is now, and the Sandbox is created after the claim.
        A Pipeline that starts at a Gate or an Operation needs no slot to claim a Ticket.
        """
        start = self.pipeline.nodes[self.pipeline.start]
        stage = start if isinstance(start, Stage) else None
        base = self.git.resolve(self.context.integration)
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

    def _mounts(self, ticket: LiveTicket) -> list[Mount]:
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

    def _advance(self, ticket: LiveTicket) -> bool:
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
            outcome, artifact = self.probes.evaluate_borrowed(ticket, node, node.from_)
        elif isinstance(node, Gate):
            if ticket.probe is None:
                self.probes.start(ticket, node)
                return True
            try:
                probe = self.probes.poll(ticket)
            except SandboxLost as error:
                self.probes.lose(ticket, node, ticket.probe.key, str(error))
                return True
            if probe is None:
                return False
            outcome, artifact = self.probes.evaluate(ticket, node, probe)
        elif isinstance(node, Operation):
            if node.takes_window and not self.window.hold(ticket.id, node.name):
                return False
            operated = self.operations.operate(ticket, node)
            if operated is None:
                return False
            outcome, artifact = operated
        else:
            raise AssertionError(f"Config refuses {node.name}, which this engine cannot run yet")
        self._leave(ticket, node, outcome, artifact)
        return True

    def _step_attempt(self, ticket: LiveTicket, node: Stage | Decision) -> Ended | bool:
        """Starts or steps the Attempt of a node, and returns whether the Ticket moved, or how the node ended.

        A failed Attempt with Attempts left in the Round is replaced on the next tick, or after the pause.
        """
        if ticket.running is None:
            # Only Attempts wait out a pause, because Probes and Operations do not use the subscription.
            return not self._held() and self.attempts.start(ticket, node)
        ended = self.attempts.step(ticket, node, lambda: self.run_start.reconcile_ticket(ticket))
        if ended is None:
            return False
        return ended if ended.outcome is not None else True

    def _leave(self, ticket: LiveTicket, node: Node, outcome: str, cause: dict[str, Any] | None) -> None:
        """Takes the route of `outcome` with `cause` for the next node.

        The route first starts the round count of the Stages it resets over.
        """
        resets = node.resets.get(outcome, ())
        if resets:
            self._append(Event.RoundsReset, {"ticket": ticket.id, "node": node.name, "stages": list(resets)})
        self._enter(ticket, self.pipeline.route(node, outcome), cause)

    def _enter(self, ticket: LiveTicket, target: str, cause: dict[str, Any] | None) -> None:
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
            self.operations.clean_up(ticket, ticket.merged_commit)
        if target == "abandoned" and ticket.state == TicketState.ABANDONED:
            self.operations.abandon(ticket)

    def _apply_commands(self) -> bool:
        """Accepts or rejects each Command in the inbox of the Run, oldest first, and returns whether any came."""
        commands = self.journal.pending_commands(self.run_id)
        for command in commands:
            try:
                match parse(command):
                    case Decide() as answer if answer.ticket == RUN_LEVEL:
                        self.run_decisions.answer(command, answer)
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

    def _ticket_of(self, ticket_id: str) -> LiveTicket:
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

    def _stop_work(self, ticket: LiveTicket, cause: str, reason: str) -> None:
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
        waited = self.decisions.take_owner_edits(ticket) if ticket.waiting is not None else None
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
            self.decisions.restart_sandbox(ticket)
        self.window.forget(ticket.id)
        self._leave(ticket, decision, outcome, artifact)

    def _set_limits(self, command: CommandRow, limits: Limits) -> None:
        """Changes the Run's parallelism and the round limits of its Stages, which count from the next Round."""
        check_limits(self.pipeline, limits)
        max_parallel, rounds, throttle = limits.max_parallel, limits.rounds, limits.throttle
        with self.journal.transaction():
            changes = [] if max_parallel is None else [f"max_parallel {max_parallel}"]
            changes += [f"{name} rounds {limit}" for name, limit in rounds.items()]
            changes += [
                f"{window} brake lifted" if shares is None else f"{window} {_shares(shares)}"
                for window, shares in throttle.items()
            ]
            self._accept(command, "", f"Limits set: {', '.join(changes)}")
            self._append(Event.LimitsChanged, {"max_parallel": max_parallel, "rounds": rounds, "throttle": throttle})

    def _check_answer(self, answer: Decide) -> tuple[LiveTicket, Decision, deciders.Ruling]:
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
        self, command: CommandRow, ticket: LiveTicket, decision: Decision, ruling: deciders.Ruling
    ) -> None:
        """Journals the accepted answer with the Decision's outcome in one transaction, then takes its route.

        The Sandbox starts again unless the route ends the Ticket.
        """
        waited = self.decisions.take_owner_edits(ticket)
        target = self.pipeline.route(decision, ruling.outcome)
        result = f"Ticket {ticket.id} decided {decision.name}: {ruling.outcome}, goes to {target}"
        with self.journal.transaction():
            self._append(
                Event.CommandAccepted,
                {"command": command.id, "kind": command.kind, "ticket": ticket.id, "result": result},
            )
            outcome, artifact = self.decisions.record(ticket, decision, ruling, waited)
        if target not in TERMINAL_NODES:
            self.decisions.restart_sandbox(ticket)
        self._leave(ticket, decision, outcome, artifact)

    def _sandbox_name(self, ticket: LiveTicket) -> str:
        return sandbox_name(self.run_id, ticket.id)

    def _append(self, type: Event, payload: dict[str, Any]) -> None:
        """Journals the event and applies it to the Run state, the only way the state changes."""
        self.journal.append(self.run_id, type, payload)
        apply(self.pipeline, self.memory, type, payload)
