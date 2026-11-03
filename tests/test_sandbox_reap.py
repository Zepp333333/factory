import os
from pathlib import Path

from factory.ports.sandbox import SandboxError
from harness import AgentScript, Harness, run_id_of
from test_fix_loop import HELLO, NOTHING, TICKET, fix_loop


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def leaves(pidfile: Path) -> AgentScript:
    """Returns an agent that leaves `sleep` running in the background and writes its pid to `pidfile`."""
    return AgentScript(output=NOTHING.output, run=[f"sh -c 'echo $$ > {pidfile}; exec sleep 300' &"])


def test_process_an_attempt_left_in_the_background_is_killed_and_journaled_before_the_next_start(
    factory: Harness,
) -> None:
    fix_loop(factory)
    pidfile = factory.root / "background.pid"
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = leaves(pidfile)
    factory.executor.scripts["fix"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [killed, *more] = factory.events(run_id, "SandboxProcessKilled")
    assert not more
    assert killed["ticket"] == "t1"
    assert killed["sandbox"] == f"{run_id}-t1"
    assert killed["command"].endswith("sleep 300")
    assert killed["pid"] == int(pidfile.read_text())
    assert killed["lifetime"] >= 0
    assert not alive(killed["pid"])
    assert factory.events(run_id, "SandboxReapFailed") == []


def test_sandbox_that_cannot_reap_is_journaled_and_the_attempt_still_runs(factory: Harness) -> None:
    def refuse(name: str) -> list[object]:
        raise SandboxError(f"Sandbox {name} cannot list its processes")

    factory.sandbox.reap = refuse  # type: ignore[method-assign, assignment]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [failed] = factory.events(run_id, "SandboxReapFailed")
    assert failed["reason"] == f"Sandbox {run_id}-t1 cannot list its processes"
    assert factory.events(run_id, "SandboxProcessKilled") == []
