import os
import signal
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from factory.cli import main
from factory.wiring import Machine
from harness import (
    EXIT_GRACE_SECONDS,
    AgentScript,
    FakeClock,
    Harness,
    group_ended,
    run_id_of,
    ticket_text,
    wait_until,
    writes,
)


def play(factory: Harness, scripts: dict[str, AgentScript]) -> None:
    for name, script in scripts.items():
        factory.executor.ticket_scripts[(name, "implement")] = script


def timeline(factory: Harness, run_id: str) -> list[str]:
    """Lists the Attempt starts and ends and the merges of a Run in order, such as `start a`, `end a`, `merged a`."""
    marks = {"AttemptStarted": "start", "AttemptFinished": "end", "OperationFinished": "merged"}
    return [
        f"{marks[type]} {event['ticket']}"
        for type, event in factory.all_events(run_id)
        if type in marks and event.get("kind", "merge") == "merge"
    ]


def test_independent_tickets_run_together_but_never_more_than_max_parallel(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    spec = factory.write_spec("Files", "Three files.", {name: ticket_text(name) for name in "abc"})
    play(factory, {name: writes(name) for name in "abc"})

    result = factory.run(spec)

    assert result.code == 0, result.out
    steps = timeline(factory, run_id_of(result.out))
    assert steps[:2] == ["start a", "start b"]
    assert steps.index("start c") > min(steps.index("end a"), steps.index("end b"))
    assert sorted(step for step in steps if step.startswith("merged")) == ["merged a", "merged b", "merged c"]


def test_ticket_starts_after_its_blockers_merged_from_the_tip_and_the_run_records_its_pr_in_the_folder(
    factory: Harness,
) -> None:
    factory.project["max_parallel"] = 3
    tickets = {"a": ticket_text("a"), "b": ticket_text("b"), "c": ticket_text("c", ["a", "b"])}
    spec = factory.write_spec("Files", "Two files and their sum.", tickets)
    play(factory, {"a": writes("a"), "b": writes("b"), "c": writes("c", "cat a.txt b.txt > c.txt")})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    steps = timeline(factory, run_id)
    assert steps.index("start c") > max(steps.index("merged a"), steps.index("merged b"))
    integration = f"factory/{run_id}"
    assert factory.git("show", f"{integration}:c.txt") == "a\nb"
    claims = {e["ticket"]: e["base"] for e in factory.events(run_id, "TicketClaimed")}
    last_merge = [e["after"] for e in factory.events(run_id, "OperationFinished") if e["kind"] == "merge"][1]
    assert claims["a"] == claims["b"] == factory.git("rev-parse", "main")
    assert claims["c"] == last_merge
    path = spec / "pull_requests" / f"factory--{run_id}.md"
    text = path.read_text()
    assert text.startswith(f"# Files: a, b, c\n\nFrom `factory/{run_id}` into `main`.\n")
    assert "- a a\n- b b\n- c c\n" in text
    assert f"Pull request: {path}" in result.out


def test_runs_on_one_machine_share_the_slot_cap_and_it_holds_back_independent_tickets_of_one_run(
    factory: Harness, tmp_path: Path
) -> None:
    factory.machine_config["slots"] = 1
    factory.project["max_parallel"] = 2
    other_run_waited = tmp_path / "other-run-waited"
    specs = []
    for name, tickets in (("a", ["a", "c"]), ("b", ["b"])):
        folder = tmp_path / f"spec-{name}"
        (folder / "tickets").mkdir(parents=True)
        (folder / "spec.md").write_text(f"# {name}\n\nWrite {name}.\n")
        for ticket in tickets:
            (folder / "tickets" / f"{ticket}.md").write_text(ticket_text(ticket))
        specs.append(folder)
    factory.remove_remotes()
    play(factory, {name: writes(name, wait_for=other_run_waited) for name in "abc"})
    codes: list[int] = []

    def run(spec: Path, machine: Machine) -> None:
        codes.append(main(["run", str(spec), "--project", str(factory.project_file)], machine))

    def machine_for(spec: Path) -> Machine:
        """Writes the config before any Run starts, since a Run that reads it mid-write sees an empty file."""
        clock = FakeClock()
        clock.on_sleep = lambda seconds: note_a_wait_without_a_slot(spec)
        return factory.machine(clock)

    def note_a_wait_without_a_slot(spec: Path) -> None:
        """Lets the agent of one Run finish only once the other Run has slept without an Attempt of its own."""
        events = factory.all_runs_events()
        [run_id] = [run_id for run_id, type, e in events if type == "RunStarted" and e["spec_ref"] == str(spec)]
        if not any(type == "AttemptStarted" and other == run_id for other, type, _ in events):
            other_run_waited.touch()

    threads = [threading.Thread(target=run, args=(spec, machine_for(spec))) for spec in specs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert not any(thread.is_alive() for thread in threads)
    assert codes == [0, 0]
    marks = {"AttemptStarted": "start", "AttemptFinished": "end"}
    steps = [marks[type] for _, type, _ in factory.all_runs_events() if type in marks]
    assert steps == ["start", "end", "start", "end", "start", "end"]


def press_ctrl_c(seconds: float) -> None:
    raise KeyboardInterrupt


def send_sigterm(seconds: float) -> None:
    os.kill(os.getpid(), signal.SIGTERM)


@pytest.mark.parametrize("stop", [press_ctrl_c, send_sigterm])
def test_run_stopped_mid_attempt_kills_its_agent_leaves_the_run_orphaned_for_finish_and_gives_back_its_slot(
    factory: Harness, stop: Callable[[float], None]
) -> None:
    factory.machine_config["slots"] = 1
    spec = factory.write_spec("Files", "One file.", {"a": ticket_text("a")})
    play(factory, {"a": AgentScript(files={"a.txt": "a\n"}, fail="hang")})
    factory.clock.on_sleep = stop

    with pytest.raises((KeyboardInterrupt, SystemExit)):
        factory.run(spec)

    [run_id] = factory.run_ids()
    assert factory.events(run_id, "RunFinished") == []
    assert factory.cli("status", run_id).out.startswith(f"Run {run_id}  orphaned")
    assert factory.slots() == []
    [agent] = factory.sandbox.started
    assert wait_until(lambda: group_ended(agent), EXIT_GRACE_SECONDS)
    assert not (factory.workspace(run_id, "a") / "late.txt").exists()

    factory.clock.on_sleep = None
    finished = factory.cli("finish", run_id)
    assert finished.code == 0, finished.out + finished.err
    status = factory.cli("status", run_id).out.splitlines()
    assert status[0].startswith(f"Run {run_id}  partial")
    cells = status[2].split()
    assert (cells[0], cells[6]) == ("a", "abandoned")
