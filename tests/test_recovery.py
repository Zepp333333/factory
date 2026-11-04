import contextlib
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from factory.journal.commands import decide_for, submit
from factory.journal.custody import HEARTBEAT_SECONDS, HEARTBEAT_STALE_SECONDS, RunCustody, RunTakenOver
from factory.journal.journal import Journal
from factory.run.orchestrator import Orchestrator
from harness import AgentScript, Harness, git, mark_ready, run_id_of, ticket_text
from test_cost_duration import query
from test_human_decider import asking
from test_ticket_assignment import LINK, published

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})
STALE = timedelta(seconds=HEARTBEAT_STALE_SECONDS + 1)


class Crash(BaseException):
    """A Crash stands for `kill -9` of the Run's process, which runs no handler."""


@pytest.fixture(autouse=True)
def killed_runs_run_no_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Makes `Crash` leave the Run as a SIGKILL would, with no `RunFinished` and the agents still running."""
    abort = Orchestrator.abort

    def abort_unless_crashed(self: Orchestrator, reason: str) -> None:
        if reason != "Crash":
            abort(self, reason)

    monkeypatch.setattr(Orchestrator, "abort", abort_unless_crashed)


def crash_when(factory: Harness, ready: Callable[[str], bool]) -> None:
    """Crashes the next Run from its sleep once `ready` says so about its id."""

    def on_sleep(seconds: float) -> None:
        if factory.run_ids() and ready(factory.run_ids()[-1]):
            factory.clock.on_sleep = None
            raise Crash

    factory.clock.on_sleep = on_sleep


def crashed_run(factory: Harness, spec: Path | str, ready: Callable[[str], bool]) -> str:
    crash_when(factory, ready)
    with pytest.raises(Crash):
        factory.run(spec)
    factory.crashed()
    return factory.run_ids()[-1]


def attempt_started(factory: Harness) -> Callable[[str], bool]:
    return lambda run_id: bool(factory.events(run_id, "AttemptStarted"))


def status_line(factory: Harness, run_id: str) -> str:
    return factory.cli("status", run_id).out.splitlines()[0]


def test_run_beats_on_the_injected_clock_and_once_it_stops_beating_is_orphaned_and_frees_its_slots(
    factory: Harness,
) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(wait_for=factory.root / "never")

    def ran_past_stale(run_id: str) -> bool:
        started = factory.times(run_id, "RunStarted")
        return bool(started) and factory.clock.now() - started[0] > STALE

    run_id = crashed_run(factory, spec, ran_past_stale)
    beats = factory.times(run_id, "Heartbeat")
    assert len(beats) >= STALE.seconds // HEARTBEAT_SECONDS
    gaps = {(later - earlier).total_seconds() for earlier, later in zip(beats, beats[1:], strict=False)}
    assert all(HEARTBEAT_SECONDS <= gap < HEARTBEAT_SECONDS + 1 for gap in gaps)
    assert factory.events(run_id, "RunOrphaned") == []
    assert factory.slots() == [(run_id, "t1")]
    assert status_line(factory, run_id).startswith(f"Run {run_id}  running")

    # The crash may come up to a beat after the last one, so the clock moves from the last beat.
    factory.clock.sleep(HEARTBEAT_STALE_SECONDS - 1 - (factory.clock.now() - beats[-1]).total_seconds())
    assert status_line(factory, run_id).startswith(f"Run {run_id}  running")
    factory.clock.sleep(2)

    assert status_line(factory, run_id).startswith(f"Run {run_id}  orphaned")
    assert factory.slots() == []
    [orphaned] = factory.events(run_id, "RunOrphaned")
    assert orphaned["silent_seconds"] > HEARTBEAT_STALE_SECONDS


def test_another_run_orphans_a_dead_run_and_takes_its_slot(factory: Harness) -> None:
    factory.machine_config = {"slots": 1}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(wait_for=factory.root / "never")
    dead = crashed_run(factory, spec, attempt_started(factory))
    factory.clock.sleep(STALE.seconds)
    factory.executor.scripts["implement"] = HELLO
    other = factory.root / "other"
    (other / "tickets").mkdir(parents=True)
    (other / "spec.md").write_text("# Other\n\nAnother.\n")
    (other / "tickets" / "t2.md").write_text(ticket_text("t2"))
    mark_ready(other, "t2")

    result = factory.run(other)

    assert result.code == 0, result.out
    assert [e["state"] for e in factory.events(run_id_of(result.out), "RunFinished")] == ["done"]
    assert len(factory.events(dead, "RunOrphaned")) == 1
    assert factory.events(dead, "RunFinished") == []
    assert status_line(factory, dead).startswith(f"Run {dead}  orphaned")


def test_resume_takes_up_only_an_orphaned_run_loses_its_attempt_in_flight_and_goes_on_from_the_journal(
    factory: Harness,
) -> None:
    factory.project["max_parallel"] = 1
    spec = factory.write_spec("Pair", "Two.", {"t1": ticket_text("t1"), "t2": ticket_text("t2", ["t1"])})
    factory.executor.scripts["implement"] = [AgentScript(files={"t1.txt": "t1\n"}, output=HELLO.output)]
    stuck = AgentScript(files={"junk.txt": "half\n"}, output=HELLO.output, wait_for=factory.root / "go")
    factory.executor.ticket_scripts[("t2", "implement")] = [
        stuck,
        AgentScript(files={"t2.txt": "t2\n"}, output=HELLO.output),
    ]
    run_id = crashed_run(factory, spec, lambda run_id: len(factory.events(run_id, "AttemptStarted")) == 2)
    first = factory.executor.requests[1]
    agent = factory.sandbox.started[-1]
    assert agent.poll() is None
    alive = factory.cli("resume", run_id)
    unknown = factory.cli("resume", "no-such-run")
    assert alive.code == unknown.code == 2
    assert f"Run {run_id} is running, and only an orphaned Run resumes" in alive.err
    assert "no Run no-such-run" in unknown.err
    assert factory.events(run_id, "RunReclaimed") == []
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert f"Run {run_id} resumed on factory/{run_id}" in result.out
    assert f"Run {run_id} finished: done" in result.out
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]
    assert factory.slots() == []

    # The merged Ticket and the Rounds of the Journal carry over, and only the Attempt in flight runs again.
    assert [e["ticket"] for e in factory.events(run_id, "TicketClaimed")] == ["t1", "t2"]
    assert factory.git("show", f"factory/{run_id}:t1.txt") == "t1"
    assert factory.git("show", f"factory/{run_id}:t2.txt") == "t2"
    assert "junk.txt" not in factory.git("ls-tree", "--name-only", f"factory/{run_id}")
    started = [(e["ticket"], e["round"], e["attempt"]) for e in factory.events(run_id, "AttemptStarted")]
    assert started == [("t1", 1, 1), ("t2", 1, 1), ("t2", 1, 2)]
    assert factory.branches().keys() == {"main", "feature", f"factory/{run_id}"}

    # The Attempt in flight is lost with unknown usage, and its unsaved work goes to a patch.
    [lost] = factory.events(run_id, "AttemptLost")
    assert (lost["ticket"], lost["node"], lost["round"], lost["attempt"]) == ("t2", "implement", 1, 1)
    assert lost["cause"] == "orchestrator_lost"
    assert lost["usage"] is None
    [(cost, duration)] = query(
        factory,
        "SELECT cost_usd, duration FROM attempts WHERE run_id = ? AND ticket_id = 't2' AND attempt = 1",
        run_id,
    )
    assert cost is None
    assert duration >= STALE.seconds
    assert "+half" in (first.attempt_dir / "discarded.patch").read_text()

    # The agent that outlived its Orchestrator was stopped and not adopted.
    assert agent.poll() is not None
    assert '"type": "result"' not in first.stream.read_text()

    # A Run that ended neither resumes nor finishes.
    done = factory.cli("resume", run_id)
    assert done.code == 2
    assert f"Run {run_id} is done, and only an orphaned Run resumes" in done.err
    ended = factory.cli("finish", run_id)
    assert ended.code == 2
    assert f"Run {run_id} is done, and only an orphaned Run finishes" in ended.err
    assert "no Run no-such-run" in factory.cli("finish", "no-such-run").err


def lose_world_mid_attempt(factory: Harness, lose: Callable[[str], None]) -> None:
    """Calls `lose` once, from the Run's sleep, when the first Attempt runs."""

    def on_sleep(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "AttemptStarted"):
            factory.clock.on_sleep = None
            lose(factory.run_ids()[-1])

    factory.clock.on_sleep = on_sleep


def test_attempts_whose_sandbox_or_workspace_vanished_are_lost_and_retried_without_spending_an_attempt(
    factory: Harness,
) -> None:
    factory.project["max_parallel"] = 2
    factory.pipeline["stages"]["implement"]["attempts"] = 1
    spec = factory.write_spec("Pair", "Two.", {"t1": ticket_text("t1"), "t2": ticket_text("t2")})
    for name in ("t1", "t2"):
        factory.executor.ticket_scripts[(name, "implement")] = [
            AgentScript(files={"junk.txt": "half\n"}, output=HELLO.output, wait_for=factory.root / "never"),
            AgentScript(files={f"{name}.txt": f"{name}\n"}, output=HELLO.output),
        ]

    def lose_both_once_both_run(seconds: float) -> None:
        run_ids = factory.run_ids()
        if run_ids and len(factory.events(run_ids[-1], "AttemptStarted")) == 2:
            factory.clock.on_sleep = None
            factory.sandbox.remove(f"{run_ids[-1]}-t1")
            git(factory.repo, "worktree", "remove", "--force", str(factory.workspace(run_ids[-1], "t2")))

    factory.clock.on_sleep = lose_both_once_both_run

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    lost = {e["ticket"]: e for e in factory.events(run_id, "AttemptLost")}
    assert (lost["t1"]["cause"], lost["t1"]["mended"], lost["t1"]["attempt"]) == ("sandbox_lost", ["sandbox"], 1)
    assert (lost["t2"]["cause"], lost["t2"]["mended"], lost["t2"]["attempt"]) == ("workspace_lost", ["workspace"], 1)
    assert factory.git("show", f"factory/{run_id}:t1.txt") == "t1"
    assert factory.git("show", f"factory/{run_id}:t2.txt") == "t2"
    assert "junk.txt" not in factory.git("ls-tree", "--name-only", f"factory/{run_id}")
    started = sorted((e["ticket"], e["attempt"]) for e in factory.events(run_id, "AttemptStarted"))
    assert started == [("t1", 1), ("t1", 2), ("t2", 1), ("t2", 2)]
    assert [e["outcome"] for e in factory.events(run_id, "AttemptFinished")] == ["done", "done"]
    status = factory.cli("status", run_id).out
    assert status.count("lost its Sandbox 1x") == 1
    assert "Ticket t1 lost its Sandbox 1x" in status


def test_stage_fails_once_its_sandbox_vanished_more_often_than_sandbox_retries(factory: Harness) -> None:
    factory.pipeline["defaults"]["sandbox_retries"] = 0
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(wait_for=factory.root / "never")

    def remove_sandbox(run_id: str) -> None:
        factory.sandbox.remove(f"{run_id}-t1")

    lose_world_mid_attempt(factory, remove_sandbox)

    result = factory.run(spec)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    [lost] = factory.events(run_id, "AttemptLost")
    assert lost["cause"] == "sandbox_lost"
    [exhausted] = factory.events(run_id, "SandboxRetriesExhausted")
    assert (exhausted["node"], exhausted["losses"], exhausted["cause"]) == ("implement", 1, "sandbox_lost")
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")] == ["implement", "abandoned"]
    assert len(factory.events(run_id, "AttemptStarted")) == 1


def test_run_orphaned_while_it_still_runs_stops_writing_to_the_journal(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(wait_for=factory.root / "never")

    def orphan_it(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "AttemptStarted"):
            factory.clock.on_sleep = None
            journal = Journal(factory.home / "factory.db", factory.clock)
            with contextlib.closing(journal):
                journal.append(factory.run_ids()[-1], "RunOrphaned", {})

    factory.clock.on_sleep = orphan_it

    result = factory.run(spec)

    run_id = factory.run_ids()[-1]
    assert result.code == 2
    assert f"Run {run_id} was orphaned by another process" in result.err
    assert factory.events(run_id, "RunFinished") == []
    assert status_line(factory, run_id).startswith(f"Run {run_id}  orphaned")


def test_cancel_and_limits_sent_to_a_dead_run_wait_in_the_inbox_and_are_applied_after_resume(factory: Harness) -> None:
    factory.project["max_parallel"] = 1
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET, "t2": ticket_text("Wave")})
    factory.executor.scripts["implement"] = HELLO
    factory.executor.ticket_scripts[("t1", "implement")] = AgentScript(wait_for=factory.root / "never")
    run_id = crashed_run(factory, spec, attempt_started(factory))
    factory.clock.sleep(STALE.seconds)
    assert status_line(factory, run_id).startswith(f"Run {run_id}  orphaned")

    cancelled = factory.cli("cancel", run_id, "t1")
    limited = factory.cli("limits", run_id, "--max-parallel", "2")

    assert cancelled.code == limited.code == 0, cancelled.err + limited.err
    assert f"the cancel waits in its inbox for `factory resume {run_id}`" in cancelled.out
    assert [state for state, _, _ in factory.commands()] == ["pending", "pending"]

    result = factory.cli("resume", run_id)

    assert result.code == 1, result.out + result.err
    assert f"Run {run_id} finished: partial" in result.out
    [(cancel_state, cancel_text, _), (limits_state, _, _)] = factory.commands()
    assert (cancel_state, cancel_text) == ("accepted", "Ticket t1 cancelled, abandoned")
    assert limits_state == "accepted"
    assert [e["ticket"] for e in factory.events(run_id, "TicketCancelled")] == ["t1"]
    assert [e["max_parallel"] for e in factory.events(run_id, "LimitsChanged")] == [2]
    assert [e["ticket"] for e in factory.events(run_id, "AttemptStarted")] == ["t1", "t2"]
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    assert factory.slots() == []


def test_run_that_crashed_while_its_decision_waited_spends_nothing_for_the_held_attempts_after_resume(
    factory: Harness,
) -> None:
    crash = AgentScript(fail="crash")
    factory.project["max_parallel"] = 2
    factory.pipeline["stages"]["implement"]["attempts"] = 2
    spec = factory.write_spec("Pair", "Two.", {"t1": ticket_text("t1"), "t2": ticket_text("t2")})
    factory.executor.scripts["implement"] = HELLO
    factory.executor.ticket_scripts[("t1", "implement")] = [crash, crash, HELLO]
    factory.executor.ticket_scripts[("t2", "implement")] = [crash, HELLO]
    run_id = crashed_run(factory, spec, lambda run_id: bool(factory.events(run_id, "DecisionWaiting")))
    journal = Journal(factory.home / "factory.db", factory.clock)
    [waiting] = journal.waiting(run_id)
    submit(journal, run_id, decide_for("@run", "retry", [], "cli", waiting))
    journal.close()
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]
    [waited] = factory.events(run_id, "DecisionWaiting")
    # The Attempt that opens the Decision is held and charged nothing, so only the other Ticket's crash gives back.
    [given] = waited["gave_back"]
    assert given["ticket"] in ("t1", "t2")
    assert given["counter"] == "spent"
    lines = factory.cli("status", run_id).out.splitlines()
    lost = {cells[0]: cells[5] for cells in (line.split() for line in lines[2:]) if cells[0] in ("t1", "t2")}
    assert lost == {"t1": "0", "t2": "0"}


def test_decide_sent_to_a_dead_run_orphans_it_waits_in_the_inbox_and_resume_applies_it_on_its_first_tick(
    factory: Harness,
) -> None:
    asking(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO
    run_id = crashed_run(factory, spec, lambda run_id: bool(factory.events(run_id, "DecisionWaiting")))
    factory.clock.sleep(STALE.seconds)
    assert factory.waiting() == ["t1"]

    sent = factory.cli("decide", run_id, "t1", "accept")

    assert sent.code == 0, sent.err
    assert f"the answer waits in its inbox for `factory resume {run_id}`" in sent.out
    assert len(factory.events(run_id, "RunOrphaned")) == 1
    assert [state for state, _, _ in factory.commands()] == ["pending"]

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert [(state, text) for state, text, _ in factory.commands()] == [
        ("accepted", "Ticket t1 decided judge: accept, goes to sync")
    ]
    assert [e["outcome"] for e in factory.events(run_id, "DecisionDecided")] == ["accept"]
    assert len(factory.events(run_id, "DecisionWaiting")) == 1
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]


def test_a_resumed_run_assigns_nothing_again(factory: Harness) -> None:
    notebook = published(factory)
    run_id = crashed_run(factory, LINK, attempt_started(factory))
    assigned = factory.events(run_id, "TicketAssigned")
    assert [event["ticket"] for event in assigned] == ["1", "2", "3"]
    # The owner takes the Tickets back, so an assignment after the resume would show.
    notebook.assignees.clear()
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert factory.events(run_id, "TicketAssigned") == assigned
    assert notebook.assignees == {}


def test_two_resumes_of_one_run_reclaim_it_once(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(wait_for=factory.root / "never")
    run_id = crashed_run(factory, spec, attempt_started(factory))
    factory.clock.sleep(STALE.seconds)
    outcomes: list[str] = []
    seen_orphaned = threading.Barrier(2, timeout=20)

    def reclaim() -> None:
        journal = Journal(factory.home / "factory.db", factory.clock)
        custody = RunCustody(journal, factory.clock)
        try:
            custody.orphan_stale()
            # Both reclaim only after each has read the Run as orphaned, so a bare state check cannot tell them apart.
            run = journal.run(run_id)
            assert run is not None and run.state == "orphaned"
            seen_orphaned.wait()
            try:
                custody.reclaim(run_id, {"in_flight": [], "probing": []})
                outcomes.append("reclaimed")
            except RunTakenOver:
                outcomes.append("taken over")
        finally:
            journal.close()

    threads = [threading.Thread(target=reclaim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(outcomes) == ["reclaimed", "taken over"]
    assert len(factory.events(run_id, "RunReclaimed")) == 1
