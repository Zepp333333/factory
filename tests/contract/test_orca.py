"""Seam 3 contract tests check that the real `orca` behaves as OrcaWorkbench and the fake `orca` assume.

They run only with FACTORY_CONTRACT=1, on a machine where the Orca app is running and its `orca` CLI is in PATH.
Each test works on a throwaway repository that Orca shows while the test runs.
"""

import json
import os
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from factory.workbench import OrcaWorkbench

pytestmark = pytest.mark.skipif(os.environ.get("FACTORY_CONTRACT") != "1", reason="set FACTORY_CONTRACT=1")


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
    repo, workspace = tmp_path / "repo", tmp_path / "workspace"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    git(repo, "branch", "ticket")
    workbench = OrcaWorkbench()
    workbench.call("status", "--json")
    stream = tmp_path / "stream.ndjson"
    stream.write_text('{"type": "result", "subtype": "success", "num_turns": 1, "duration_ms": 1, "result": "ok"}\n')
    try:
        yield Bench(workbench, repo, workspace, stream)
    finally:
        if workspace.exists():
            workbench.remove_workspace(repo, workspace)
        workbench.call("repo", "rm", "--repo", f"path:{repo}", "--json")


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
    terminal = bench.workbench.open_attempt(bench.stream, log, bench.workspace, "implement")
    deadline = time.monotonic() + 30
    while not log.exists() or "result success" not in log.read_text():
        assert time.monotonic() < deadline, "the renderer showed no result"
        time.sleep(0.2)
    terminal.close()
    assert not terminal.alive()
    json.loads(bench.workbench.call("terminal", "list", "--worktree", f"path:{bench.workspace}", "--json"))
