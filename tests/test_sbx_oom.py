import shlex
from pathlib import Path

from harness import AgentScript, Harness, run_id_of, wait_until
from test_infra_pause import Answerer
from test_sbx_sandbox import sbx_project

PEAK = 1000000000


def oom_and_die(factory: Harness, peak: int | None = PEAK) -> str:
    """Returns a shell command that makes the VM's OOM killer count a kill and then kills the agent with SIGKILL."""
    return shlex.join([*factory.sbx.command, "oom", *([str(peak)] if peak else [])]) + "; kill -9 $PPID"


def test_sandbox_tells_an_oom_kill_by_its_count_and_a_second_one_in_a_row_opens_a_run_decision(
    factory: Harness,
) -> None:
    """The OOM killer, a SIGKILL, the engine on timeout and twice more the OOM killer end the agent."""
    spec = sbx_project(factory)
    factory.pipeline["defaults"].update({"attempts": 3, "timeout": "5s"})
    oom = shlex.join([*factory.sbx.command, "oom"])
    factory.executor.scripts["implement"] = [
        AgentScript(run=[oom_and_die(factory, None)]),
        AgentScript(run=["kill -9 $PPID"]),
        AgentScript(files={"junk.txt": "half\n"}, run=[oom], fail="hang"),
        AgentScript(run=[oom_and_die(factory)]),
    ]
    answerer = Answerer(factory, "abort")

    result = factory.run(spec)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    finished = factory.events(run_id, "AttemptFinished")
    assert [e["cause"] for e in finished] == ["oom", "executor_crash", "timeout", "oom", "oom"]
    no_peak, crash, timeout, _, last = finished
    assert (no_peak["memory_limit"], "memory_peak" in no_peak) == ("8g", False)
    assert "exited with code 137" in no_peak["reason"]
    assert "memory_limit" not in crash
    assert "memory_limit" not in timeout
    assert (last["memory_limit"], last["memory_peak"]) == ("8g", PEAK)
    # Only the crash and the timeout spend one of the 3 attempts, so all five Attempts run in Round 1.
    assert [e.get("charge") for e in finished] == [None, "spent", "spent", None, None]
    assert {e["round"] for e in finished} == {1}

    # The engine's kill on timeout ends the agent and the tool it started in the Sandbox.
    hung = factory.executor.requests[2].attempt_dir
    assert not (hung / "exit-code").exists()
    assert not wait_until((factory.workspace(run_id, "t1") / "late.txt").exists, 0.5)

    [waiting] = factory.events(run_id, "DecisionWaiting")
    assert waiting["ticket"] == "@run" and waiting["cause"] == "oom"
    assert "raise sandbox.memory in the project config" in Path(waiting["card"]).read_text()
    answerer.output()
    status = factory.cli("status", run_id).out
    assert f"Ticket t1 ran out of memory in its Sandbox (limit 8g, peak {PEAK} bytes): " in status
    assert "raise sandbox.memory in the project config" in status
