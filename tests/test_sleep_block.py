import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness import AgentScript, Harness, ticket_text

DEADLINE_SECONDS = 15


def fake_helpers(factory: Harness, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Puts a `caffeinate` and a `systemd-inhibit` first on the PATH that record how they were started and then idle.

    A helper writes its pid first and its arguments last, so the arguments file says it has started.
    """
    bin = factory.root / "helper-bin"
    bin.mkdir()
    for name in ("caffeinate", "systemd-inhibit"):
        script = bin / name
        script.write_text(
            "#!/bin/sh\n"
            f"echo $$ > {bin}/{name}.pid\n"
            f'echo "$@" > {bin}/{name}.args.tmp && mv {bin}/{name}.args.tmp {bin}/{name}.args\n'
            "exec sleep 300\n"
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin}{os.pathsep}{os.environ['PATH']}")
    return bin


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + DEADLINE_SECONDS
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.01)


def started_after(bin: Path, name: str) -> str:
    """Returns the arguments the helper `name` was started with, once it has started."""
    wait_for(bin / f"{name}.args")
    return (bin / f"{name}.args").read_text().strip()


def assert_gone(bin: Path, name: str) -> None:
    pid = int((bin / f"{name}.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def waits_for_helper(bin: Path, name: str) -> AgentScript:
    """Returns an agent that works only once the helper `name` runs, so the Run cannot end before it started."""
    command = f"until [ -e {bin}/{name}.args ]; do sleep 0.01; done"
    return AgentScript(files={"a.txt": "a\n"}, run=[command], output={"summary": "Add a", "handoff": "done"})


def one_ticket(factory: Harness) -> Path:
    factory.project["prevent_sleep"] = True
    return factory.write_spec("Files", "One file.", {"a": ticket_text("a")})


# What each helper is started with, where `{pid}` is the Run's process.
HELPERS = [
    ("darwin", "caffeinate", "-i -w {pid}"),
    ("linux", "systemd-inhibit", "--what=idle:sleep --who=factory --why=A Run is going tail --pid={pid} -f /dev/null"),
]


@pytest.mark.parametrize(("platform", "name", "expected"), HELPERS, ids=[platform for platform, _, _ in HELPERS])
def test_run_blocks_idle_sleep_tied_to_its_pid_and_releases_it_at_the_end(
    factory: Harness, monkeypatch: pytest.MonkeyPatch, platform: str, name: str, expected: str
) -> None:
    bin = fake_helpers(factory, monkeypatch)
    factory.platform = platform
    spec = one_ticket(factory)
    factory.executor.scripts["implement"] = waits_for_helper(bin, name)

    result = factory.run(spec)

    [run_id] = factory.run_ids()
    assert result.code == 0, result.out + result.err
    assert started_after(bin, name) == expected.format(pid=os.getpid())
    assert [e["argv"][0] for e in factory.events(run_id, "SleepBlocked")] == [name]
    assert factory.events(run_id, "SleepBlockUnavailable") == []
    assert "sleep" not in result.err
    assert_gone(bin, name)


def test_run_stopped_with_ctrl_c_releases_the_block(factory: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    bin = fake_helpers(factory, monkeypatch)
    factory.platform = "darwin"
    spec = one_ticket(factory)
    factory.executor.scripts["implement"] = AgentScript(wait_for=factory.root / "never")

    def press_ctrl_c_once_blocked(seconds: float) -> None:
        wait_for(bin / "caffeinate.args")
        raise KeyboardInterrupt

    factory.clock.on_sleep = press_ctrl_c_once_blocked

    with pytest.raises(KeyboardInterrupt):
        factory.run(spec)

    assert_gone(bin, "caffeinate")


def test_run_that_crashes_releases_the_block(factory: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    bin = fake_helpers(factory, monkeypatch)
    factory.platform = "darwin"
    spec = one_ticket(factory)
    factory.executor.scripts["implement"] = AgentScript(wait_for=factory.root / "never")

    def fail_once_blocked(seconds: float) -> None:
        wait_for(bin / "caffeinate.args")
        raise RuntimeError("boom")

    factory.clock.on_sleep = fail_once_blocked

    with pytest.raises(RuntimeError):
        factory.run(spec)

    assert_gone(bin, "caffeinate")


def test_run_goes_on_and_says_why_once_when_the_platform_has_no_way_to_block_sleep(factory: Harness) -> None:
    factory.platform = "freebsd"
    spec = one_ticket(factory)
    factory.executor.scripts["implement"] = AgentScript(
        files={"a.txt": "a\n"}, output={"summary": "Add a", "handoff": "done"}
    )

    result = factory.run(spec)

    [run_id] = factory.run_ids()
    reason = "no way to block sleep is known on freebsd"
    assert result.code == 0, result.out + result.err
    assert result.err.count(reason) == 1
    assert [e["reason"] for e in factory.events(run_id, "SleepBlockUnavailable")] == [reason]
    assert factory.events(run_id, "SleepBlocked") == []


def test_run_goes_on_and_names_the_helper_that_is_not_installed(
    factory: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("factory.sleep_block.shutil", SimpleNamespace(which=lambda name: None))
    factory.platform = "darwin"
    spec = one_ticket(factory)
    factory.executor.scripts["implement"] = AgentScript(
        files={"a.txt": "a\n"}, output={"summary": "Add a", "handoff": "done"}
    )

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert "caffeinate is not installed" in result.err


@pytest.mark.parametrize("off", ["config", "flag"])
def test_block_is_turned_off_in_the_project_file_and_with_a_flag(
    factory: Harness, monkeypatch: pytest.MonkeyPatch, off: str
) -> None:
    bin = fake_helpers(factory, monkeypatch)
    factory.platform = "darwin"
    spec = one_ticket(factory)
    factory.project["prevent_sleep"] = off != "config"
    factory.executor.scripts["implement"] = AgentScript(
        files={"a.txt": "a\n"}, output={"summary": "Add a", "handoff": "done"}
    )

    result = factory.run(spec, *(["--no-prevent-sleep"] if off == "flag" else []))

    [run_id] = factory.run_ids()
    assert result.code == 0, result.out + result.err
    assert factory.events(run_id, "SleepBlocked") == []
    assert factory.events(run_id, "SleepBlockUnavailable") == []
    assert not (bin / "caffeinate.pid").exists()


def test_resume_blocks_idle_sleep_like_run(factory: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    bin = fake_helpers(factory, monkeypatch)
    factory.platform = "darwin"
    spec = one_ticket(factory)
    factory.executor.scripts["implement"] = [
        AgentScript(wait_for=factory.root / "never"),
        waits_for_helper(bin, "caffeinate"),
    ]

    def press_ctrl_c_once_blocked(seconds: float) -> None:
        wait_for(bin / "caffeinate.args")
        raise KeyboardInterrupt

    factory.clock.on_sleep = press_ctrl_c_once_blocked
    with pytest.raises(KeyboardInterrupt):
        factory.run(spec)
    factory.clock.on_sleep = None
    [run_id] = factory.run_ids()
    for leftover in bin.glob("caffeinate.*"):
        leftover.unlink()

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert started_after(bin, "caffeinate") == f"-i -w {os.getpid()}"
    assert len(factory.events(run_id, "SleepBlocked")) == 2
    assert_gone(bin, "caffeinate")


def test_project_file_refuses_a_prevent_sleep_that_is_not_a_boolean(factory: Harness) -> None:
    factory.project["prevent_sleep"] = "never"

    result = factory.check()

    assert result.code == 2
    assert "prevent_sleep: must be true or false" in result.err
