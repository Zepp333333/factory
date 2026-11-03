"""The contract of the Workbench port, which every Workbench kind of `wiring` and of the harness passes."""

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from factory.config import DEFAULT_EXECUTOR
from factory.ports.workbench import Workbench, WorkbenchError
from factory.wiring import REGISTRIES
from hosts import Host, host, kinds, made_kind  # noqa: F401

# The host CLI each kind shows its Workspaces with, which `hosts` fakes, or None for one that needs none.
FAKED = {"terminal": None, "orca": "orca"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")
STREAM = [
    {"type": "assistant", "message": {"id": "msg_1", "content": [{"type": "text", "text": "Done."}]}},
    {"type": "result", "subtype": "success", "num_turns": 1, "duration_ms": 1, "result": "ok"},
]


@dataclass
class Bench:
    workbench: Workbench
    repo: Path
    path: Path


@pytest.fixture(params=kinds(REGISTRIES.workbenches, harness=True), ids=str)
def bench(request: pytest.FixtureRequest, host: Host, tmp_path: Path) -> Iterator[Bench]:  # noqa: F811
    """Gives the kind's Workbench and the path of a Workspace on the branch `ticket`, which it removes if left."""
    workbench: Workbench = made_kind(request.param, host, "workbenches", FAKED)
    host.factory.git("branch", "ticket")
    made = Bench(workbench, host.factory.repo, tmp_path / "workspaces" / "t1")
    yield made
    if workbench.has_workspace(made.repo, made.path):
        host.factory.git("worktree", "remove", "--force", str(made.path))


def test_a_workspace_is_there_from_its_creation_to_its_removal(bench: Bench) -> None:
    assert not bench.workbench.has_workspace(bench.repo, bench.path)

    bench.workbench.create_workspace(bench.repo, bench.path, "ticket")
    assert bench.workbench.has_workspace(bench.repo, bench.path)
    assert (bench.path / "README.md").is_file()
    bench.workbench.mark_stage(bench.path, "implement, Round 1")

    bench.workbench.remove_workspace(bench.repo, bench.path)
    assert not bench.workbench.has_workspace(bench.repo, bench.path)
    assert not bench.path.exists()


def test_remove_refuses_a_workspace_with_edits_and_keeps_it(bench: Bench) -> None:
    bench.workbench.create_workspace(bench.repo, bench.path, "ticket")
    (bench.path / "draft.txt").write_text("not committed\n")

    with pytest.raises(WorkbenchError):
        bench.workbench.remove_workspace(bench.repo, bench.path)

    assert bench.workbench.has_workspace(bench.repo, bench.path)
    assert (bench.path / "draft.txt").read_text() == "not committed\n"


def test_an_attempt_terminal_shows_the_streams_lines_in_its_log_until_it_closes(bench: Bench) -> None:
    bench.workbench.create_workspace(bench.repo, bench.path, "ticket")
    attempt = bench.path.parent / "attempt"
    attempt.mkdir()
    stream, log = attempt / "stream.ndjson", attempt / "terminal.log"
    stream.write_text("".join(json.dumps(event) + "\n" for event in STREAM))

    terminal = bench.workbench.open_attempt(stream, log, bench.path, "implement", DEFAULT_EXECUTOR)
    terminal.close()

    assert not terminal.alive()
    shown = ANSI.sub("", log.read_text()).splitlines()
    assert "Done." in shown
    assert any(line.startswith("result success") for line in shown)
