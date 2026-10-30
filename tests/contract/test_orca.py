"""Seam 3 contract tests check that the real `orca` behaves as OrcaWorkbench and the fake `orca` assume.

They run only with FACTORY_CONTRACT=1, on a machine where the Orca app is running and its `orca` CLI is in PATH.
The tests share one test repository that stays in place across runs, so Orca lists it once,
because the `orca` CLI cannot remove a repository from Orca.
Orca stops seeing new worktrees of a repository that was deleted and made again at its path, so the tests never do that.
"""

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from factory.workbench import OrcaTerminal, OrcaWorkbench

pytestmark = pytest.mark.skipif(os.environ.get("FACTORY_CONTRACT") != "1", reason="set FACTORY_CONTRACT=1")

REPO = Path(tempfile.gettempdir()) / "factory-orca-contract" / "repo"


@dataclass
class Bench:
    workbench: OrcaWorkbench
    repo: Path
    workspace: Path
    stream: Path


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    repo, workspace = REPO, tmp_path / "workspace"
    if not (repo / ".git").exists():
        repo.mkdir(parents=True)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    git(repo, "worktree", "prune")
    git(repo, "branch", "-f", "ticket", "main")
    workbench = OrcaWorkbench()
    workbench.call("status", "--json")
    stream = tmp_path / "stream.ndjson"
    stream.write_text('{"type": "result", "subtype": "success", "num_turns": 1, "duration_ms": 1, "result": "ok"}\n')
    try:
        yield Bench(workbench, repo, workspace, stream)
    finally:
        if workspace.exists():
            workbench.remove_workspace(repo, workspace)


def test_orca_shows_the_workspace_and_removes_it(bench: Bench) -> None:
    bench.workbench.create_workspace(bench.repo, bench.workspace, "ticket")

    assert bench.workbench.has_workspace(bench.repo, bench.workspace)

    bench.workbench.remove_workspace(bench.repo, bench.workspace)
    assert not bench.workbench.has_workspace(bench.repo, bench.workspace)


def test_orca_terminal_runs_the_renderer_and_shows_the_stage_on_the_card(bench: Bench) -> None:
    bench.workbench.create_workspace(bench.repo, bench.workspace, "ticket")

    bench.workbench.mark_stage(bench.workspace, "implement, Round 1")
    shown = bench.workbench.call("worktree", "ps", "--json")
    assert "implement, Round 1" in shown

    log = bench.workspace.parent / "terminal.log"
    terminal = bench.workbench.open_attempt(bench.stream, log, bench.workspace, "implement", "claude-code")
    deadline = time.monotonic() + 30
    while not log.exists() or "result success" not in log.read_text():
        assert time.monotonic() < deadline, "the renderer showed no result"
        time.sleep(0.2)
    deadline = time.monotonic() + 10
    while terminal.alive():
        assert time.monotonic() < deadline, "the terminal outlived its renderer"
        time.sleep(0.2)
    terminal.close()
    json.loads(bench.workbench.call("terminal", "list", "--worktree", f"path:{bench.workspace}", "--json"))


def test_orca_closes_a_terminal_whose_command_does_not_end_it(bench: Bench) -> None:
    bench.workbench.create_workspace(bench.repo, bench.workspace, "ticket")
    created = bench.workbench.call(
        "terminal", "create", "--worktree", f"path:{bench.workspace}", "--title", "stuck", "--command", "true", "--json"
    )
    handle = json.loads(created)["result"]["terminal"]["handle"]
    terminal = OrcaTerminal(bench.workbench, bench.workspace, handle, bench.workspace.parent / "terminal.log")

    terminal.close()

    assert not terminal.alive()
