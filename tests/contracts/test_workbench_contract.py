"""The contract of the Workbench port, which every Workbench kind of `wiring` and of the harness passes."""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from factory.ports.executor import attempt_event, event_line
from factory.ports.workbench import RunTree, TicketTree, Workbench, WorkbenchError
from factory.wiring import REGISTRIES
from harness import wait_until
from hosts import Host, host, kinds, made_kind  # noqa: F401

# The host CLI each kind shows its Workspaces with, which `hosts` fakes, or None for one that needs none.
FAKED = {"terminal": None, "orca": "orca"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")
EVENTS = [
    attempt_event("progress"),
    attempt_event("text", kind="message", text="Done."),
    attempt_event("result", status="success", turns=1, duration_ms=1, text="ok"),
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


def test_an_attempt_terminal_shows_the_lines_of_the_attempt_events_in_its_log_until_it_closes(bench: Bench) -> None:
    bench.workbench.create_workspace(bench.repo, bench.path, "ticket")
    attempt = bench.path.parent / "attempt"
    attempt.mkdir()
    events, log = attempt / "events.ndjson", attempt / "terminal.log"
    events.write_bytes(b"".join(event_line(event) for event in EVENTS))

    terminal = bench.workbench.open_attempt(events, log, bench.path, "implement")
    terminal.close()

    assert not terminal.alive()
    shown = ANSI.sub("", log.read_text()).splitlines()
    assert "Done." in shown
    assert any(line.startswith("result success") for line in shown)


def test_an_attempt_terminal_is_alive_until_it_closes_or_its_renderer_shows_the_result(bench: Bench) -> None:
    bench.workbench.create_workspace(bench.repo, bench.path, "ticket")
    attempt = bench.path.parent / "attempt"
    attempt.mkdir()
    waiting, ended = attempt / "waiting.ndjson", attempt / "ended.ndjson"
    waiting.write_text("")
    ended.write_bytes(event_line(EVENTS[-1]))

    terminal = bench.workbench.open_attempt(waiting, attempt / "waiting.log", bench.path, "implement")
    assert terminal.alive()
    terminal.close()
    assert not terminal.alive()

    terminal = bench.workbench.open_attempt(ended, attempt / "ended.log", bench.path, "implement")
    assert wait_until(lambda: not terminal.alive(), 10), "the terminal outlived its renderer"


def test_the_run_tree_and_the_need_for_the_owner_reach_a_workbench_without_a_terminal_answering(bench: Bench) -> None:
    ticket = TicketTree("t1", "active", "judge", 1)
    bench.workbench.show_run(RunTree("run", "running", "working", (ticket,), ("t1",)))
    bench.workbench.needs_owner(True)
    bench.workbench.needs_owner(False)
    bench.workbench.show_run(RunTree("run", "done", "done", (TicketTree("t1", "merged", None, 1),), ()))
