import json
from datetime import timedelta

from factory.orchestrator import RATE_LIMIT_PAUSE, TICK_SECONDS
from harness import RESETS_AT, AgentScript, Harness, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
DONE = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})
LIMITED = AgentScript(files={"junk.txt": "half\n"}, fail="rate_limited")


def test_rate_limited_attempt_spends_no_attempt_and_the_next_one_starts_at_resets_at(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"].update({"attempts": 1, "rounds": {"max": 1}})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [LIMITED, DONE]

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    assert "junk.txt" not in factory.git("ls-tree", "--name-only", f"factory/{run_id}")
    first, second = factory.executor.requests
    record = json.loads((first.attempt_dir / "attempt.json").read_text())
    assert (record["outcome"], record["cause"]) == ("failed", "rate_limited")
    assert "+half" in (first.attempt_dir / "discarded.patch").read_text()
    assert [json.loads(r.brief.read_text())["round"] for r in (first, second)] == [1, 1]
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")] == ["implement", "sync", "merge", "done"]
    types = [type for type, _ in factory.all_events(run_id)]
    pause = types.index("RunPaused")
    assert types[pause - 1 : pause + 3] == ["AttemptFinished", "RunPaused", "RunResumed", "AttemptStarted"]
    [paused] = factory.events(run_id, "RunPaused")
    assert paused == {
        "resets_at": RESETS_AT.isoformat(),
        "ticket": "t1",
        "node": "implement",
        "round": 1,
        "attempt": 1,
        "reason": "five_hour limit rejected the agent",
    }
    assert factory.events(run_id, "RunResumed") == [{"resets_at": RESETS_AT.isoformat()}]
    assert factory.times(run_id, "RunResumed") == [RESETS_AT]
    assert factory.times(run_id, "AttemptStarted")[1] == RESETS_AT


def test_rate_limit_without_resets_at_waits_the_fixed_pause(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [AgentScript(fail="rate_limited", resets_at=None), DONE]

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    finished = factory.times(run_id, "AttemptFinished")[0]
    resumes = finished + timedelta(seconds=RATE_LIMIT_PAUSE)
    assert factory.events(run_id, "RunPaused")[0]["resets_at"] == resumes.isoformat()
    assert factory.times(run_id, "AttemptStarted")[1] == resumes


def test_resets_at_that_already_passed_waits_the_fixed_pause(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    stale = AgentScript(fail="rate_limited", resets_at=RESETS_AT - timedelta(days=1))
    factory.executor.scripts["implement"] = [stale, DONE]

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    finished = factory.times(run_id, "AttemptFinished")[0]
    assert factory.times(run_id, "AttemptStarted")[1] == finished + timedelta(seconds=RATE_LIMIT_PAUSE)


def test_rate_limits_do_not_hide_the_failures_that_count(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["attempts"] = 2
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    crash = AgentScript(fail="crash")
    factory.executor.scripts["implement"] = [crash, LIMITED, LIMITED, crash, DONE]

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    causes = [e["cause"] for e in factory.events(run_id, "AttemptFinished")]
    assert causes == ["executor_crash", "rate_limited", "rate_limited", "executor_crash"]
    row = factory.cli("status", run_id).out.splitlines()[2]
    assert row.split() == ["t1", "abandoned", "implement", "1", "4", "0", "abandoned"]


def test_status_shows_the_pause_while_the_run_waits(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [LIMITED, DONE]
    seen: list[str] = []

    def look(seconds: float) -> None:
        if seconds > TICK_SECONDS and not seen:
            [run_id] = factory.run_ids()
            seen.append(factory.cli("status", run_id).out)

    factory.clock.on_sleep = look

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [during] = seen
    head = f"Run {run_id}  running  factory/{run_id}  paused until {RESETS_AT.isoformat()}"
    assert during.splitlines()[0] == head
    assert f"paused until {RESETS_AT.isoformat()}" in result.out
    assert factory.cli("status", run_id).out.splitlines()[0] == f"Run {run_id}  done  factory/{run_id}"
