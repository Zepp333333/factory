"""The charge of an Attempt is the Attempt module's rule, so these tests drive it with a fake Executor and Sandbox.

Only the module's own interface is used: `start`, `step` and `lose`.
Git and the Journal's slots are real, and every event the module appends is applied to the Run state,
as the Orchestrator does.
"""

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from factory.artifacts import RunDir
from factory.attempt import RATE_LIMIT_PAUSE, Attempts, Ended, Ports
from factory.config import Project
from factory.events import Event
from factory.executor import AttemptHandle, AttemptRequest, AttemptResult, Failure
from factory.git import HostGit
from factory.journal import Journal
from factory.pipeline import Pipeline, Role, Stage
from factory.run_state import Conflict, InfraFailure, RunMemory, TicketRuntime, apply
from factory.sandbox import MemoryReport, Mount, Process, Reaped, Sandbox, SandboxError, SandboxSettings
from factory.states import SANDBOX_LOSSES, SPENT, TicketState
from factory.workbench import AttemptTerminal, WorkbenchError
from harness import FakeClock

SCHEMA = {"type": "object", "required": ["handoff"], "properties": {"handoff": {"type": "string"}}}
DONE = {"handoff": "done"}


class FakeSandbox:
    def check(self) -> None:
        pass

    def create(self, name: str, workspace: Path, mounts: Sequence[Mount]) -> None:
        pass

    def attach(self, name: str, workspace: Path) -> bool:
        return True

    def start(self, name: str) -> None:
        pass

    def stop(self, name: str) -> None:
        pass

    def remove(self, name: str) -> str | None:
        return None

    def reap(self, name: str) -> list[Reaped]:
        return []

    def exec(
        self, name: str, argv: list[str], stdout: Path, env: dict[str, str] | None = None, stderr: Path | None = None
    ) -> Process:
        raise SandboxError("the fake runs nothing")


class FakeWorkbench:
    def create_workspace(self, repo: Path, path: Path, branch: str) -> None:
        pass

    def remove_workspace(self, repo: Path, path: Path) -> None:
        pass

    def has_workspace(self, repo: Path, path: Path) -> bool:
        return True

    def mark_stage(self, path: Path, text: str) -> None:
        pass

    def open_attempt(self, stream: Path, log: Path, workspace: Path, title: str) -> AttemptTerminal:
        raise WorkbenchError("the fake has no terminal")


class ScriptedHandle:
    def __init__(self, result: AttemptResult | None) -> None:
        self.result = result
        self.killed = False

    def poll(self) -> AttemptResult | None:
        return self.result

    def kill(self) -> None:
        self.killed = True


@dataclass
class FakeExecutor:
    """FakeExecutor answers every Attempt with `result`, after it writes `files` into the Workspace."""

    result: AttemptResult | None = None
    files: dict[str, str] = field(default_factory=dict)
    refuses: bool = False
    handles: list[ScriptedHandle] = field(default_factory=list)

    def reads(self) -> list[Path]:
        return []

    def start(self, request: AttemptRequest, sandbox: Sandbox) -> AttemptHandle:
        if self.refuses:
            raise SandboxError("the Sandbox did not start")
        for name, text in self.files.items():
            (request.workspace / name).write_text(text)
        handle = ScriptedHandle(self.result)
        self.handles.append(handle)
        return handle


@dataclass
class World:
    """World is a Target project with one worktree per Ticket, and the Attempt module over a fake Executor."""

    root: Path
    executor: FakeExecutor
    clock: FakeClock
    memory: RunMemory
    attempts: Attempts
    events: list[tuple[str, dict[str, Any]]]
    asked: list[str]
    run_dir: RunDir
    run_id: str = "run"

    def ticket(self, id: str = "t1") -> TicketRuntime:
        workspace = self.root / "workspaces" / id
        git("worktree", "add", "-q", "-b", f"factory/run--{id}", str(workspace), "main", cwd=self.root / "repo")
        base = git("rev-parse", "HEAD", cwd=workspace)
        ticket = TicketRuntime(id=id, blocked_by=[], state=TicketState.ACTIVE, node="implement", round=1)
        ticket.rounds_since_reset["implement"] = 1
        ticket.branch, ticket.base, ticket.synced, ticket.workspace = f"factory/run--{id}", base, base, workspace
        self.memory.tickets.append(ticket)
        return ticket

    def run(self, ticket: TicketRuntime) -> Ended:
        """Starts the Ticket's Attempt and steps it to its end."""
        node = self.attempts.pipeline.nodes["implement"]
        assert isinstance(node, Stage)
        assert self.attempts.start(ticket, node)
        ended = self.attempts.step(ticket, node, lambda: False)
        assert ended is not None
        return ended


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def world(tmp_path: Path) -> World:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "t", cwd=repo)
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "README.md").write_text("hello\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    (tmp_path / "prompt.md").write_text("prompt")
    (tmp_path / "schema.json").write_text(json.dumps(SCHEMA))
    role = Role("implementer", tmp_path / "prompt.md", tmp_path / "schema.json")
    stage = Stage("implement", role, "sonnet", (), {"done": "done"}, attempts=2, timeout=60, commits=True)
    pipeline = Pipeline(tmp_path / "pipeline", "implement", {"implement": stage}, None, sandbox_retries=1)
    project = Project(tmp_path / "factory.yaml", repo, "main", pipeline, SandboxSettings())
    clock = FakeClock()
    executor = FakeExecutor()
    memory = RunMemory([], 1)
    events: list[tuple[str, dict[str, Any]]] = []
    asked: list[str] = []

    def append(type: Event, payload: dict[str, Any]) -> None:
        events.append((type, payload))
        apply(pipeline, memory, type, payload)

    class Scope:
        run_id = "run"
        run_dir = RunDir(tmp_path / "run")

    scope = Scope()
    scope.memory = memory  # type: ignore[attr-defined]
    attempts = Attempts(
        Journal(tmp_path / "journal.db", clock),
        append,
        project,
        pipeline,
        Ports(executor, FakeSandbox(), FakeWorkbench(), clock),
        HostGit(repo),
        scope,  # type: ignore[arg-type]
        slots=4,
        tick=0.2,
        beat=lambda: None,
        ask_run=asked.append,
    )
    return World(tmp_path, executor, clock, memory, attempts, events, asked, scope.run_dir)


def failed(cause: Failure, **more: Any) -> AttemptResult:
    return AttemptResult(output=None, failure=cause, reason=f"{cause} for the test", **more)


def test_done_attempt_spends_nothing_and_leaves_its_artifact(world: World) -> None:
    world.executor.result = AttemptResult(output=DONE, failure=None)
    world.executor.files = {"hello.txt": "hello\n"}
    ticket = world.ticket()

    ended = world.run(ticket)

    assert (ended.outcome, ended.charge) == ("done", None)
    assert ended.artifact is not None and ended.artifact["head_after"] != ended.artifact["head_before"]
    assert ticket.spent == 0
    assert ticket.artifacts == [ended.artifact]


def test_rate_limited_spends_nothing_and_pauses_the_run_until_the_reset(world: World) -> None:
    resets = datetime(2026, 10, 20, 9, 20, tzinfo=world.clock.now().tzinfo)
    world.executor.result = failed("rate_limited", resets_at=resets)
    ticket = world.ticket()

    ended = world.run(ticket)

    assert (ended.outcome, ended.charge) == (None, None)
    assert (ticket.spent, ticket.sandbox_losses) == (0, 0)
    assert world.memory.paused_until == resets


def test_rate_limited_without_a_reset_time_pauses_for_the_fixed_time(world: World) -> None:
    world.executor.result = failed("rate_limited")
    ticket = world.ticket()

    world.run(ticket)

    assert world.memory.paused_until == world.clock.now() + timedelta(seconds=RATE_LIMIT_PAUSE)


def test_oom_spends_nothing_and_journals_the_sandboxs_memory(world: World) -> None:
    world.executor.result = failed("oom", memory=MemoryReport(oom_kills=1, limit="1g", peak=1024))
    ticket = world.ticket()

    ended = world.run(ticket)

    assert (ended.outcome, ended.charge) == (None, None)
    assert (ticket.spent, ticket.sandbox_losses) == (0, 0)
    assert world.asked == []
    [(_, payload)] = [e for e in world.events if e[0] == Event.AttemptFinished]
    assert (payload["cause"], payload["memory_limit"], payload["memory_peak"]) == ("oom", "1g", 1024)


def test_a_second_oom_of_one_ticket_in_a_row_opens_the_runs_decision(world: World) -> None:
    world.memory.failures = [InfraFailure("t1", "oom", "implement", 1, None)]
    world.executor.result = failed("oom")
    ticket = world.ticket()

    ended = world.run(ticket)

    assert (ended.outcome, ended.charge) == (None, None)
    assert ticket.spent == 0
    assert world.asked == ["oom"]


def test_sandbox_start_counts_as_a_sandbox_loss_and_spends_no_attempt(world: World) -> None:
    world.executor.refuses = True
    ticket = world.ticket()

    ended = world.run(ticket)

    assert (ended.outcome, ended.charge) == (None, SANDBOX_LOSSES)
    assert (ticket.spent, ticket.sandbox_losses) == (0, 1)


def test_sandbox_lost_counts_as_a_sandbox_loss_and_spends_no_attempt(world: World) -> None:
    world.executor.result = failed("sandbox_lost")
    ticket = world.ticket()

    ended = world.run(ticket)

    assert (ended.outcome, ended.charge) == (None, SANDBOX_LOSSES)
    assert (ticket.spent, ticket.sandbox_losses) == (0, 1)


def test_the_node_fails_once_the_sandbox_was_lost_more_often_than_sandbox_retries(world: World) -> None:
    world.executor.result = failed("sandbox_lost")
    ticket = world.ticket()
    ticket.sandbox_losses = 1

    ended = world.run(ticket)

    assert (ended.outcome, ended.charge) == ("failed", SANDBOX_LOSSES)
    assert ticket.spent == 0
    [(_, payload)] = [e for e in world.events if e[0] == Event.SandboxRetriesExhausted]
    assert (payload["losses"], payload["sandbox_retries"], payload["cause"]) == (2, 1, "sandbox_lost")


@pytest.mark.parametrize("cause", ["auth", "executor_crash", "agent_config"])
def test_the_agents_own_failure_spends_an_attempt(world: World, cause: Failure) -> None:
    world.executor.result = failed(cause)
    ticket = world.ticket()

    ended = world.run(ticket)

    assert (ended.outcome, ended.charge) == (None, SPENT)
    assert (ticket.spent, ticket.sandbox_losses) == (1, 0)


def test_timeout_kills_the_agent_and_spends_an_attempt(world: World) -> None:
    world.executor.result = None
    ticket = world.ticket()
    node = world.attempts.pipeline.nodes["implement"]
    assert isinstance(node, Stage)
    assert world.attempts.start(ticket, node)
    assert world.attempts.step(ticket, node, lambda: False) is None
    world.clock.sleep(node.timeout)

    ended = world.attempts.step(ticket, node, lambda: False)

    assert ended == Ended(None, charge=SPENT)
    assert world.executor.handles[0].killed
    assert ticket.spent == 1
    [(_, payload)] = [e for e in world.events if e[0] == Event.AttemptFinished]
    assert payload["cause"] == "timeout"


def test_the_last_attempt_of_the_round_fails_the_node(world: World) -> None:
    world.executor.result = failed("executor_crash")
    ticket = world.ticket()
    ticket.spent = 1

    ended = world.run(ticket)

    assert (ended.outcome, ended.charge) == ("failed", SPENT)


def test_output_that_breaks_the_schema_fails_as_executor_crash_and_spends_an_attempt(world: World) -> None:
    world.executor.result = AttemptResult(output={"handoff": 1}, failure=None)
    ticket = world.ticket()

    ended = world.run(ticket)

    assert (ended.outcome, ended.charge) == (None, SPENT)
    [(_, payload)] = [e for e in world.events if e[0] == Event.AttemptFinished]
    assert payload["cause"] == "executor_crash" and payload["errors"]


def test_ignored_files_fail_a_committing_stage_and_spend_an_attempt(world: World) -> None:
    world.executor.result = AttemptResult(output=DONE, failure=None)
    world.executor.files = {"build.log": "noise\n"}
    ticket = world.ticket()

    ended = world.run(ticket)

    assert (ended.outcome, ended.charge) == (None, SPENT)
    [(_, payload)] = [e for e in world.events if e[0] == Event.AttemptFinished]
    assert payload["cause"] == "ignored_files" and payload["removed_ignored"] == ["build.log"]
    assert ticket.spent == 1


def test_conflict_markers_left_by_a_committing_stage_fail_it_and_spend_an_attempt(world: World) -> None:
    world.executor.result = AttemptResult(output=DONE, failure=None)
    world.executor.files = {"README.md": "<<<<<<< ours\nhello\n=======\nhi\n>>>>>>> theirs\n"}
    ticket = world.ticket()
    assert ticket.base is not None
    ticket.conflict = Conflict(tip=ticket.base, files=["README.md"])

    ended = world.run(ticket)

    assert (ended.outcome, ended.charge) == (None, SPENT)
    [(_, payload)] = [e for e in world.events if e[0] == Event.AttemptFinished]
    assert payload["cause"] == "conflict_markers" and payload["errors"] == ["README.md still holds conflict markers"]
    assert ticket.spent == 1


def test_an_attempt_held_by_the_runs_decision_spends_nothing_and_opens_it(world: World) -> None:
    world.memory.failures = [InfraFailure("t0", "auth", "implement", 1, SPENT)]
    world.executor.result = failed("auth")
    ticket = world.ticket()

    ended = world.run(ticket)

    assert (ended.outcome, ended.charge) == (None, None)
    assert ticket.spent == 0
    assert world.asked == ["auth"]


def test_a_lost_sandbox_counts_toward_sandbox_retries_but_a_lost_process_does_not(world: World) -> None:
    ticket = world.ticket()
    world.executor.result = None
    node = world.attempts.pipeline.nodes["implement"]
    assert isinstance(node, Stage)
    assert world.attempts.start(ticket, node)
    key = {"ticket": "t1", "node": "implement", "round": 1, "attempt": 1, "head_before": ticket.base}

    assert world.attempts.lose(ticket, key, "orchestrator_lost", "the Run's process died", [], {}) is None
    assert ticket.sandbox_losses == 0
    assert world.attempts.lose(ticket, key, "sandbox_lost", "gone", [], {}) is None
    assert ticket.sandbox_losses == 1
    assert world.attempts.lose(ticket, key, "sandbox_lost", "gone again", [], {}) == Ended(
        "failed", charge=SANDBOX_LOSSES
    )
    assert (ticket.spent, ticket.sandbox_losses) == (0, 2)
