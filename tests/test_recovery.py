import contextlib
import subprocess
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from factory.commands import decide_for, submit
from factory.custody import HEARTBEAT_SECONDS, HEARTBEAT_STALE_SECONDS, RunCustody, RunTakenOver
from factory.journal import Journal
from factory.orchestrator import Orchestrator
from harness import AgentScript, Harness, git, run_id_of, ticket_text
from test_human_decider import asking

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
    return factory.run_ids()[-1]


def attempt_started(factory: Harness) -> Callable[[str], bool]:
    return lambda run_id: bool(factory.events(run_id, "AttemptStarted"))


def status_line(factory: Harness, run_id: str) -> str:
    return factory.cli("status", run_id).out.splitlines()[0]


def test_run_that_stopped_beating_is_orphaned_and_frees_its_slots(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    hang = Path(factory.root / "never")
    factory.executor.scripts["implement"] = AgentScript(wait_for=hang)
    run_id = crashed_run(factory, spec, attempt_started(factory))
    assert factory.slots() == [(run_id, "t1")]
    assert status_line(factory, run_id).startswith(f"Run {run_id}  running")

    factory.clock.sleep(HEARTBEAT_STALE_SECONDS - 1)
    assert status_line(factory, run_id).startswith(f"Run {run_id}  running")
    factory.clock.sleep(2)

    assert status_line(factory, run_id).startswith(f"Run {run_id}  orphaned")
    assert factory.slots() == []
    [orphaned] = factory.events(run_id, "RunOrphaned")
    assert orphaned["silent_seconds"] > HEARTBEAT_STALE_SECONDS


def test_running_run_beats_on_the_injected_clock(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    gate = factory.root / "go"
    factory.executor.scripts["implement"] = AgentScript(files=HELLO.files, output=HELLO.output, wait_for=gate)

    def release_after_a_minute(seconds: float) -> None:
        if factory.run_ids() and (factory.clock.now() - factory.times(factory.run_ids()[-1], "RunStarted")[0]) > STALE:
            gate.touch()

    factory.clock.on_sleep = release_after_a_minute

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    beats = factory.times(run_id, "Heartbeat")
    assert len(beats) >= STALE.seconds // HEARTBEAT_SECONDS
    gaps = {(later - earlier).total_seconds() for earlier, later in zip(beats, beats[1:], strict=False)}
    assert all(HEARTBEAT_SECONDS <= gap < HEARTBEAT_SECONDS + 1 for gap in gaps)
    assert factory.events(run_id, "RunOrphaned") == []


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

    result = factory.run(other)

    assert result.code == 0, result.out
    assert [e["state"] for e in factory.events(run_id_of(result.out), "RunFinished")] == ["done"]
    assert len(factory.events(dead, "RunOrphaned")) == 1
    assert factory.events(dead, "RunFinished") == []
    assert status_line(factory, dead).startswith(f"Run {dead}  orphaned")


def test_resume_loses_the_attempt_in_flight_stops_its_agent_and_goes_on_from_the_journal(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    gate = factory.root / "go"
    stuck = AgentScript(files={"junk.txt": "half\n"}, output=HELLO.output, wait_for=gate)
    factory.executor.scripts["implement"] = [stuck, HELLO]
    run_id = crashed_run(factory, spec, attempt_started(factory))
    first = factory.executor.requests[0]
    [agent] = factory.sandbox.started
    assert agent.poll() is None
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert f"Run {run_id} resumed on factory/{run_id}" in result.out
    assert f"Run {run_id} finished: done" in result.out
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    assert "junk.txt" not in factory.git("ls-tree", "--name-only", f"factory/{run_id}")
    [lost] = factory.events(run_id, "AttemptLost")
    assert (lost["node"], lost["round"], lost["attempt"], lost["cause"]) == ("implement", 1, 1, "orchestrator_lost")
    assert "+half" in (first.attempt_dir / "discarded.patch").read_text()
    assert [e["attempt"] for e in factory.events(run_id, "AttemptStarted")] == [1, 2]
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]
    assert factory.slots() == []

    # The agent that outlived its Orchestrator was stopped and not adopted.
    assert agent.poll() is not None
    assert '"type": "result"' not in first.stream.read_text()


def test_resume_takes_up_only_an_orphaned_run(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [AgentScript(wait_for=factory.root / "never"), HELLO]
    run_id = crashed_run(factory, spec, attempt_started(factory))

    alive = factory.cli("resume", run_id)
    unknown = factory.cli("resume", "no-such-run")

    assert alive.code == unknown.code == 2
    assert f"Run {run_id} is running, and only an orphaned Run resumes" in alive.err
    assert "no Run no-such-run" in unknown.err
    assert factory.events(run_id, "RunReclaimed") == []

    factory.clock.sleep(STALE.seconds)
    assert factory.cli("resume", run_id).code == 0
    done = factory.cli("resume", run_id)
    assert done.code == 2
    assert f"Run {run_id} is done, and only an orphaned Run resumes" in done.err


def test_resume_restores_a_waiting_decision_and_its_answer_in_the_inbox(factory: Harness) -> None:
    asking(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    run_id = crashed_run(factory, spec, lambda run_id: bool(factory.waiting()))
    factory.clock.sleep(STALE.seconds)
    assert factory.waiting() == ["t1"]
    answer: list[subprocess.Popen[str]] = []

    def answer_once_resumed(seconds: float) -> None:
        if not answer and factory.events(run_id, "RunReclaimed"):
            answer.append(factory.spawn("decide", run_id, "t1", "accept"))

    factory.clock.on_sleep = answer_once_resumed

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    out, err = answer[0].communicate(timeout=30)
    assert answer[0].returncode == 0, err
    assert out.strip() == "Ticket t1 decided judge: accept, goes to sync"
    assert [e["outcome"] for e in factory.events(run_id, "DecisionDecided")] == ["accept"]
    assert len(factory.events(run_id, "DecisionWaiting")) == 1
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]


def test_resume_keeps_rounds_and_merged_tickets_of_the_journal(factory: Harness) -> None:
    factory.project["max_parallel"] = 1
    spec = factory.write_spec("Pair", "Two.", {"t1": ticket_text("t1"), "t2": ticket_text("t2", ["t1"])})
    factory.executor.scripts["implement"] = [AgentScript(files={"t1.txt": "t1\n"}, output=HELLO.output)]
    factory.executor.ticket_scripts[("t2", "implement")] = [
        AgentScript(wait_for=factory.root / "never"),
        AgentScript(files={"t2.txt": "t2\n"}, output=HELLO.output),
    ]
    run_id = crashed_run(factory, spec, lambda run_id: len(factory.events(run_id, "AttemptStarted")) == 2)
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert [e["ticket"] for e in factory.events(run_id, "TicketClaimed")] == ["t1", "t2"]
    assert factory.git("show", f"factory/{run_id}:t1.txt") == "t1"
    assert factory.git("show", f"factory/{run_id}:t2.txt") == "t2"
    assert [(e["ticket"], e["round"]) for e in factory.events(run_id, "AttemptStarted")] == [
        ("t1", 1),
        ("t2", 1),
        ("t2", 1),
    ]
    assert factory.branches().keys() == {"main", "feature", f"factory/{run_id}"}


def lose_world_mid_attempt(factory: Harness, lose: Callable[[str], None]) -> None:
    """Calls `lose` once, from the Run's sleep, when the first Attempt runs."""

    def on_sleep(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "AttemptStarted"):
            factory.clock.on_sleep = None
            lose(factory.run_ids()[-1])

    factory.clock.on_sleep = on_sleep


def lost_attempt_run(factory: Harness, lose: Callable[[str], None]) -> tuple[str, dict[str, object]]:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [
        AgentScript(files={"junk.txt": "half\n"}, output=HELLO.output, wait_for=factory.root / "never"),
        HELLO,
    ]
    lose_world_mid_attempt(factory, lose)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [lost] = factory.events(run_id, "AttemptLost")
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    assert "junk.txt" not in factory.git("ls-tree", "--name-only", f"factory/{run_id}")
    assert [e["attempt"] for e in factory.events(run_id, "AttemptStarted")] == [1, 2]
    assert [e["outcome"] for e in factory.events(run_id, "AttemptFinished")] == ["done"]
    return run_id, lost


def test_attempt_whose_sandbox_vanished_is_lost_and_retried_without_spending_an_attempt(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["attempts"] = 1

    def remove_sandbox(run_id: str) -> None:
        factory.sandbox.remove(f"{run_id}-t1")

    run_id, lost = lost_attempt_run(factory, remove_sandbox)

    assert (lost["cause"], lost["mended"]) == ("sandbox_lost", ["sandbox"])
    assert lost["attempt"] == 1
    assert factory.cli("status", run_id).out.count("lost its Sandbox 1x") == 1


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


def test_attempt_whose_workspace_vanished_is_lost_and_the_workspace_made_again(factory: Harness) -> None:
    def remove_workspace(run_id: str) -> None:
        git(factory.repo, "worktree", "remove", "--force", str(factory.workspace(run_id, "t1")))

    run_id, lost = lost_attempt_run(factory, remove_workspace)

    assert (lost["cause"], lost["mended"]) == ("workspace_lost", ["workspace"])
    assert lost["attempt"] == 1


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


def test_cancel_sent_to_a_dead_run_waits_in_the_inbox_and_is_applied_after_resume(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(wait_for=factory.root / "never")
    run_id = crashed_run(factory, spec, attempt_started(factory))
    factory.clock.sleep(STALE.seconds)
    assert status_line(factory, run_id).startswith(f"Run {run_id}  orphaned")

    sent = factory.cli("cancel", run_id, "t1")

    assert sent.code == 0, sent.err
    assert f"the cancel waits in its inbox for `factory resume {run_id}`" in sent.out
    assert [state for state, _, _ in factory.commands()] == ["pending"]

    result = factory.cli("resume", run_id)

    assert result.code == 1, result.out + result.err
    assert f"Run {run_id} finished: partial" in result.out
    assert [(state, text) for state, text, _ in factory.commands()] == [("accepted", "Ticket t1 cancelled, abandoned")]
    assert [e["ticket"] for e in factory.events(run_id, "TicketCancelled")] == ["t1"]
    assert [e["attempt"] for e in factory.events(run_id, "AttemptStarted")] == [1]
    assert factory.slots() == []


def test_limits_sent_to_a_dead_run_are_applied_after_resume(factory: Harness) -> None:
    factory.project["max_parallel"] = 1
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET, "t2": ticket_text("Wave")})
    gate = factory.root / "go"
    factory.executor.scripts["implement"] = HELLO
    factory.executor.ticket_scripts[("t1", "implement")] = [
        AgentScript(wait_for=factory.root / "never"),
        AgentScript(files=HELLO.files, output=HELLO.output, wait_for=gate),
    ]
    run_id = crashed_run(factory, spec, attempt_started(factory))
    factory.clock.sleep(STALE.seconds)
    assert status_line(factory, run_id).startswith(f"Run {run_id}  orphaned")
    assert factory.cli("limits", run_id, "--max-parallel", "2").code == 0

    def release_once_both_run(seconds: float) -> None:
        started = [e["ticket"] for e in factory.events(run_id, "AttemptStarted")]
        if "t2" in started and started.count("t1") == 2:
            gate.touch()

    factory.clock.on_sleep = release_once_both_run

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert [e["max_parallel"] for e in factory.events(run_id, "LimitsChanged")] == [2]


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
    lost = {cells[0]: cells[-2] for cells in (line.split() for line in lines[2:]) if cells[0] in ("t1", "t2")}
    assert lost == {"t1": "0", "t2": "0"}


def test_decide_sent_to_a_dead_run_orphans_it_waits_in_the_inbox_and_resume_applies_it_on_its_first_tick(
    factory: Harness,
) -> None:
    asking(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO
    run_id = crashed_run(factory, spec, lambda run_id: bool(factory.events(run_id, "DecisionWaiting")))
    factory.clock.sleep(STALE.seconds)

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
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]


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
