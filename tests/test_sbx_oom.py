import shlex
from pathlib import Path

from harness import AgentScript, Harness, run_id_of
from test_infra_pause import Answerer, done
from test_sbx_sandbox import HELLO, TICKET, sbx_project

PEAK = 1000000000


def oom_and_die(factory: Harness, peak: int | None = PEAK) -> str:
    """Returns a shell command that makes the VM's OOM killer count a kill and then kills the agent with SIGKILL."""
    return shlex.join([*factory.sbx.command, "oom", *([str(peak)] if peak else [])]) + "; kill -9 $PPID"


def test_agent_the_oom_killer_killed_ends_oom_and_spends_no_attempt(factory: Harness) -> None:
    spec = sbx_project(factory, memory="1g")
    factory.pipeline["stages"]["implement"]["attempts"] = 1
    factory.executor.scripts["implement"] = [AgentScript(run=[oom_and_die(factory)]), HELLO]

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [first, second] = factory.events(run_id, "AttemptFinished")
    assert (first["cause"], first["memory_limit"], first["memory_peak"]) == ("oom", "1g", PEAK)
    assert "exited with code 137" in first["reason"]
    assert first.get("charge") is None
    assert second["outcome"] == "done"


def test_peak_usage_is_left_out_of_the_journal_when_the_sandbox_shows_none(factory: Harness) -> None:
    spec = sbx_project(factory)
    factory.executor.scripts["implement"] = [AgentScript(run=[oom_and_die(factory, None)]), HELLO]

    run_id = run_id_of(factory.run(spec).out)

    [first, _] = factory.events(run_id, "AttemptFinished")
    assert first["cause"] == "oom" and first["memory_limit"] == "8g" and "memory_peak" not in first


def test_sigkill_without_an_oom_kill_is_an_executor_crash(factory: Harness) -> None:
    spec = sbx_project(factory)
    factory.pipeline["stages"]["implement"]["attempts"] = 2
    factory.executor.scripts["implement"] = [AgentScript(run=["kill -9 $PPID"]), HELLO]

    run_id = run_id_of(factory.run(spec).out)

    [first, _] = factory.events(run_id, "AttemptFinished")
    assert first["cause"] == "executor_crash" and "memory_limit" not in first


def test_kill_of_the_engine_on_timeout_is_no_oom_even_after_an_oom_kill(factory: Harness) -> None:
    spec = sbx_project(factory)
    factory.pipeline["defaults"].update({"attempts": 2, "timeout": "5s"})
    oom = shlex.join([*factory.sbx.command, "oom"])
    factory.executor.scripts["implement"] = [AgentScript(run=[oom], fail="hang"), HELLO]

    run_id = run_id_of(factory.run(spec).out)

    [first, _] = factory.events(run_id, "AttemptFinished")
    assert first["cause"] == "timeout"


def test_second_oom_in_a_row_of_one_ticket_opens_a_run_decision_and_status_advises_more_memory(
    factory: Harness,
) -> None:
    spec = sbx_project(factory, memory="1g")
    factory.pipeline["stages"]["implement"]["attempts"] = 1
    factory.executor.scripts["implement"] = AgentScript(run=[oom_and_die(factory)])
    answerer = Answerer(factory, "abort")

    result = factory.run(spec)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    [waiting] = factory.events(run_id, "DecisionWaiting")
    assert waiting["ticket"] == "@run" and waiting["cause"] == "oom"
    assert "raise sandbox.memory in the project config" in Path(waiting["card"]).read_text()
    assert [e["cause"] for e in factory.events(run_id, "AttemptFinished")] == ["oom", "oom"]
    answerer.output()
    status = factory.cli("status", run_id).out
    assert f"Ticket t1 ran out of memory in its Sandbox (limit 1g, peak {PEAK} bytes): " in status
    assert "raise sandbox.memory in the project config" in status


def test_ooms_of_different_tickets_open_a_run_decision(factory: Harness) -> None:
    factory.project["sandbox"] = {"kind": "sbx"}
    factory.project["max_parallel"] = 2
    spec = factory.write_spec("Pair", "Two.", {"t1": TICKET, "t2": TICKET})
    factory.executor.scripts["implement"] = [AgentScript(run=[oom_and_die(factory)]), done("x")]
    answerer = Answerer(factory, "abort")

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    [waiting] = factory.events(run_id, "DecisionWaiting")
    assert waiting["cause"] == "oom"
    answerer.output()
