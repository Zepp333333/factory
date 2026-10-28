import os
import shutil
import stat
from pathlib import Path

import pytest

from harness import AgentScript, Harness, ticket_text
from test_recovery import HELLO, lose_world_mid_attempt, status_line

XCODE = "You have not agreed to the Xcode license agreements. Please run 'sudo xcodebuild -license'."


def fake_git(factory: Harness, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Puts a `git` first on the PATH that fails `worktree` calls with the Xcode message while `broken` exists."""
    real = shutil.which("git")
    broken = factory.root / "git-broken"
    bin = factory.root / "fake-bin"
    bin.mkdir()
    script = bin / "git"
    script.write_text(
        "#!/bin/sh\n"
        f'if [ -e "{broken}" ]; then\n'
        '  for arg in "$@"; do\n'
        f'    if [ "$arg" = worktree ]; then echo "{XCODE}" >&2; exit 69; fi\n'
        "  done\n"
        "fi\n"
        f'exec "{real}" "$@"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin}{os.pathsep}{os.environ['PATH']}")
    return broken


def test_host_tool_failure_leaves_the_run_orphaned_and_resume_keeps_the_ticket(
    factory: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = fake_git(factory, monkeypatch)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("t1")})
    release = factory.root / "release"
    factory.executor.scripts["implement"] = [AgentScript(wait_for=release), HELLO]
    lose_world_mid_attempt(factory, lambda _: broken.touch())

    result = factory.run(spec)

    [run_id] = factory.run_ids()
    assert result.code == 2
    assert "git worktree list --porcelain" in result.err
    assert XCODE in result.err
    assert result.err.splitlines()[-2:] == [f"factory resume {run_id}", f"factory finish {run_id}"]
    assert factory.events(run_id, "RunFinished") == []
    assert status_line(factory, run_id).startswith(f"Run {run_id}  orphaned")
    assert factory.slots() == []

    broken.unlink()
    release.touch()
    resumed = factory.cli("resume", run_id)

    assert resumed.code == 0, resumed.out + resumed.err
    assert status_line(factory, run_id).startswith(f"Run {run_id}  done")
    [lost] = factory.events(run_id, "AttemptLost")
    assert (lost["ticket"], lost["node"], lost["round"], lost["attempt"]) == ("t1", "implement", 1, 1)
    assert [(e["ticket"], e["round"], e["attempt"]) for e in factory.events(run_id, "AttemptStarted")] == [
        ("t1", 1, 1),
        ("t1", 1, 2),
    ]
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]
