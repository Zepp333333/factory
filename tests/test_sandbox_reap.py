import os
import shlex
import sys
from pathlib import Path

import pytest

from factory.sandbox import SandboxError
from harness import AgentScript, Harness, run_id_of
from test_fix_loop import HELLO, NOTHING, TICKET, fix_loop

# Starts `sleep` in a session of its own, so only a Sandbox that kills by environment finds it.
DETACHED = "import subprocess, sys; subprocess.Popen(sys.argv[1:], start_new_session=True)"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def leaves(pidfile: Path, detached: bool = False) -> AgentScript:
    """Returns an agent that leaves `sleep` running in the background and writes its pid to `pidfile`."""
    sleep = f"echo $$ > {pidfile}; exec sleep 300"
    launch = shlex.join([sys.executable, "-c", DETACHED, "sh", "-c", sleep]) if detached else f"sh -c '{sleep}' &"
    return AgentScript(output=NOTHING.output, run=[launch])


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


# The fake `sbx` runs the reap script on the host, and the script finds processes through `/proc`, which only Linux has.
@pytest.mark.skipif(not Path("/proc/self/environ").exists(), reason="the sbx reap script reads /proc")
def test_reap_kills_a_detached_process_in_the_sbx_sandbox_and_spares_the_current_attempt(factory: Harness) -> None:
    factory.project["sandbox"] = {"kind": "sbx"}
    fix_loop(factory)
    pidfile = factory.root / "background.pid"
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = leaves(pidfile, detached=True)
    factory.executor.scripts["fix"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [killed, *more] = factory.events(run_id, "SandboxProcessKilled")
    assert not more
    assert killed["command"].strip().endswith("sleep 300")
    assert killed["pid"] == int(pidfile.read_text())
    assert not alive(killed["pid"])
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"


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
