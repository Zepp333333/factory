"""The contract of the Sandbox port, which every Sandbox kind of `wiring` and of the harness passes."""

import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from factory.config import DEFAULT_EXECUTOR
from factory.ports.sandbox import Mount, Needs, Process, Sandbox, SandboxError
from factory.wiring import REGISTRIES, made
from harness import wait_until
from hosts import Host, host, kinds, made_kind  # noqa: F401

# The host CLI each kind runs its Sandboxes with, which `hosts` fakes, or None for one that needs none.
FAKED = {"local": None, "sbx": "sbx"}
NAME = "s1"


@dataclass
class Bench:
    sandbox: Sandbox
    workspace: Path
    out: Path

    def exec(self, script: str, env: dict[str, str] | None = None) -> Process:
        return self.sandbox.exec(NAME, ["sh", "-c", script], stdout=self.out / "stdout.txt", env=env)


def ended(process: Process) -> int:
    deadline = time.monotonic() + 10
    while (code := process.poll()) is None:
        assert time.monotonic() < deadline, "the process did not end"
        time.sleep(0.01)
    return code


def alive(pid: int) -> bool:
    """Tells whether the process runs, where a zombie that waits for its parent to reap it does not.

    A child that outlived its command's leader dies a moment after the SIGKILL of its group, so tests poll for it.
    """
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


@pytest.fixture(params=kinds(REGISTRIES.sandboxes, harness=True), ids=str)
def sandbox(request: pytest.FixtureRequest, host: Host) -> Sandbox:  # noqa: F811
    made_sandbox: Sandbox = made_kind(request.param, host, "sandboxes", FAKED)
    made_sandbox.require(made(REGISTRIES.executors[DEFAULT_EXECUTOR], host.tools).needs())
    return made_sandbox


@pytest.fixture
def bench(sandbox: Sandbox, tmp_path: Path) -> Iterator[Bench]:
    """Creates the Sandbox on a Workspace of its own, which sees a read-only and a writable mount, and removes it."""
    workspace, out, shared = tmp_path / "workspace", tmp_path / "out", tmp_path / "shared"
    for path in (workspace, out, shared):
        path.mkdir()
    (shared / "ticket.md").write_text("Write hello.txt.\n")
    sandbox.create(NAME, workspace, [Mount(shared), Mount(out, writable=True)])
    yield Bench(sandbox, workspace, out)
    sandbox.remove(NAME)


def test_require_adds_to_the_needs_and_refuses_a_variable_set_twice(sandbox: Sandbox) -> None:
    sandbox.require(Needs(egress=("pypi.org",), env={"TOOLCHAIN": "swift-6"}))

    with pytest.raises(SandboxError):
        sandbox.require(Needs(env={"TOOLCHAIN": "swift-5"}))
    assert "pypi.org" in sandbox.needs.egress
    assert sandbox.needs.env["TOOLCHAIN"] == "swift-6"


def test_check_passes_on_a_machine_that_runs_the_sandboxes(sandbox: Sandbox) -> None:
    sandbox.check()


def test_exec_runs_the_command_in_the_workspace_with_its_env_and_tells_its_exit_code(bench: Bench) -> None:
    bench.sandbox.require(Needs(env={"TOOLCHAIN": "swift-6"}))

    process = bench.exec('printf "%s %s" "$TOOLCHAIN" "$GREETING" > said.txt; exit 3', env={"GREETING": "hello"})

    assert ended(process) == 3
    assert (bench.workspace / "said.txt").read_text() == "swift-6 hello"


def test_the_command_reads_a_mount_and_writes_only_to_a_writable_one(bench: Bench, tmp_path: Path) -> None:
    process = bench.exec(f"cat {tmp_path / 'shared' / 'ticket.md'} > {bench.out / 'copy.md'}")

    assert ended(process) == 0
    assert (bench.out / "copy.md").read_text() == "Write hello.txt.\n"


def test_kill_stops_the_command_and_its_children_and_returns_once_they_are_gone(bench: Bench) -> None:
    pidfile = bench.out / "child.pid"
    process = bench.exec(f"sleep 30 & echo $! > {pidfile}; wait")
    deadline = time.monotonic() + 10
    while not pidfile.exists() or not pidfile.read_text().strip():
        assert time.monotonic() < deadline, "the command did not start its child"
        time.sleep(0.01)

    process.kill()

    assert process.poll() is not None
    assert wait_until(lambda: not alive(int(pidfile.read_text())), 5)


def test_reap_leaves_nothing_running_that_an_earlier_exec_left_in_its_process_group(bench: Bench) -> None:
    """A Sandbox may stop such a process when its command ends, before `reap`, and then it reports none."""
    pidfile = bench.out / "left.pid"
    process = bench.exec(f"sleep 300 & echo $! > {pidfile}")
    assert ended(process) == 0

    reaped = bench.sandbox.reap(NAME)

    assert wait_until(lambda: not alive(int(pidfile.read_text())), 5)
    assert wait_until(lambda: not any(alive(r.pid) for r in reaped), 5)
    assert all(r.lifetime >= 0 for r in reaped)
    assert bench.sandbox.reap(NAME) == []


def test_stop_kills_what_runs_in_the_sandbox_and_start_lets_it_run_commands_again(bench: Bench) -> None:
    process = bench.exec("sleep 0.3; touch late.txt; sleep 30")

    bench.sandbox.stop(NAME)

    assert process.poll() is not None
    assert not wait_until((bench.workspace / "late.txt").exists, 0.5)
    bench.sandbox.start(NAME)
    assert ended(bench.exec("true")) == 0
