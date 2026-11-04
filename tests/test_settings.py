"""Scenarios of the Run settings and their Sources: built-in, the Factory config, the project file and `--set`."""

import contextlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from factory.config import display_path
from factory.ports.kind import Kind, Tools
from factory.ports.sandbox import Needs
from harness import AgentScript, Harness, ScriptedExecutor, run_id_of
from test_mixed_executors import Home, parse_home
from test_recovery import STALE, Crash, attempt_started, crash_when, killed_runs_run_no_handler  # noqa: F401

OUTPUT = {"summary": "s", "handoff": "h"}


def pair(factory: Harness) -> Path:
    """Writes a Spec folder of two Tickets, the second blocked by the first, whose Stages each add a line."""
    spec = factory.write_spec(
        "Pair",
        "Two.",
        {"t1": "---\ntitle: One\n---\nFirst.\n", "t2": "---\ntitle: Two\nblocked_by: [t1]\n---\nSecond.\n"},
    )
    factory.executor.scripts["implement"] = AgentScript(run=["echo line >> log.txt"], output=OUTPUT)
    return spec


def rewrite_started(factory: Harness, run_id: str, forget: Callable[[dict[str, Any]], None]) -> None:
    """Has `forget` take keys out of the Run's `RunStarted`, as a Journal from before they were recorded has it."""
    with contextlib.closing(sqlite3.connect(factory.home / "factory.db")) as db, db:
        [(seq, payload)] = db.execute(
            "SELECT seq, payload FROM events WHERE run_id = ? AND type = 'RunStarted'", (run_id,)
        )
        started = json.loads(payload)
        forget(started)
        db.execute("UPDATE events SET payload = ? WHERE seq = ?", (json.dumps(started), seq))


def forget_settings(factory: Harness, run_id: str) -> None:
    """Takes the settings out of the Run's `RunStarted`."""

    def forget(started: dict[str, Any]) -> None:
        del started["settings"], started["settings_unlike_file"]

    rewrite_started(factory, run_id, forget)


def forget_executors(factory: Harness, run_id: str) -> None:
    """Takes the `executors` block out of the settings of the Run's `RunStarted`."""

    def forget(started: dict[str, Any]) -> None:
        del started["settings"]["executors"]

    rewrite_started(factory, run_id, forget)


def crashed(factory: Harness, spec: Path, *flags: str) -> str:
    """Crashes a Run of `spec` in its first Attempt, and returns its id once it is orphaned."""
    line = AgentScript(run=["echo line >> log.txt"], output=OUTPUT)
    factory.executor.ticket_scripts[("t1", "implement")] = [AgentScript(wait_for=factory.root / "never"), line]
    crash_when(factory, attempt_started(factory))
    with pytest.raises(Crash):
        factory.run(spec, *flags)
    factory.crashed()
    factory.clock.sleep(STALE.seconds)
    return factory.run_ids()[-1]


def pr_files(spec: Path) -> list[str]:
    return sorted(path.name for path in (spec / "pull_requests").glob("*.md"))


def test_set_landing_lands_a_stack_over_the_project_file(factory: Harness) -> None:
    spec = pair(factory)

    result = factory.run(spec, "--set", "landing=stack")

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert pr_files(spec) == [f"01-factory--{run_id}--1-one.md", f"02-factory--{run_id}--2-two.md"]
    status = factory.cli("status", run_id)
    assert "Settings unlike the project file: landing=stack" in status.out


def test_a_resumed_run_lands_as_it_recorded_after_the_project_file_changed(factory: Harness) -> None:
    factory.project["landing"] = "stack"
    spec = pair(factory)
    run_id = crashed(factory, spec)
    factory.project["landing"] = "single"

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert pr_files(spec) == [f"01-factory--{run_id}--1-one.md", f"02-factory--{run_id}--2-two.md"]
    assert "Settings unlike" not in factory.cli("status", run_id).out


def test_a_journal_without_recorded_settings_resumes_from_the_project_file(factory: Harness) -> None:
    factory.project["landing"] = "stack"
    spec = pair(factory)
    run_id = crashed(factory, spec)
    forget_settings(factory, run_id)
    factory.project["landing"] = "single"

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert pr_files(spec) == [f"factory--{run_id}.md"]


def executors_made(factory: Harness) -> list[Home]:
    """Registers the Executor kind `fake`, which takes a `home`, and returns the settings it is made with."""
    made: list[Home] = []

    def make(settings: Home, tools: Tools) -> ScriptedExecutor:
        made.append(settings)
        return factory.executor

    factory.registries.executors.register("fake", Kind(make, ("home",), parse_home))
    return made


def test_a_resumed_run_hands_its_executors_the_settings_it_recorded_after_the_project_file_changed(
    factory: Harness,
) -> None:
    made = executors_made(factory)
    factory.project.update({"executor": "fake", "executors": {"fake": {"home": "/recorded"}}})
    spec = pair(factory)
    run_id = crashed(factory, spec)
    factory.project["executors"] = {"fake": {"home": "/later"}}
    made.clear()

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    # `Machine.kinds` also makes each kind with a bare block, for its checks.
    assert set(made) == {Home("/default"), Home("/recorded")}
    assert factory.events(run_id, "RunStarted")[0]["settings"]["executors"] == {"fake": {"home": "/recorded"}}


def test_a_journal_from_before_the_block_executors_resumes_without_its_settings(factory: Harness) -> None:
    made = executors_made(factory)
    factory.project["executor"] = "fake"
    spec = pair(factory)
    run_id = crashed(factory, spec)
    forget_executors(factory, run_id)
    factory.project["executors"] = {"fake": {"home": "/later"}}
    made.clear()

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert set(made) == {Home("/default")}


def test_a_resumed_run_takes_sandbox_env_from_the_project_file_as_the_journal_never_holds_its_values(
    factory: Harness,
) -> None:
    factory.project["sandbox"] = {"env": {"GREETING": "hi"}}
    spec = pair(factory)
    run_id = crashed(factory, spec)
    [started] = factory.events(run_id, "RunStarted")
    assert started["settings"]["sandbox"] == {}
    factory.sandbox.needs = Needs()

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert factory.sandbox.needs.env["GREETING"] == "hi"


def test_the_project_file_wins_over_the_machine_and_set_over_the_project_file(factory: Harness) -> None:
    factory.machine_config = {"slots": 4, "landing": "stack", "max_parallel": 3}
    factory.project["max_parallel"] = 2
    spec = pair(factory)

    result = factory.run(spec, "--set", "max_parallel=1")

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert pr_files(spec) == [f"01-factory--{run_id}--1-one.md", f"02-factory--{run_id}--2-two.md"]
    [started] = factory.events(run_id, "RunStarted")
    assert (started["max_parallel"], started["settings"]["landing"]) == (1, "stack")
    status = factory.cli("status", run_id).out
    assert "Settings unlike the project file: max_parallel=1, landing=stack" in status


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("landing=pile", "landing: must be one of single, stack"),
        ("ci.timeout=soon", "ci.timeout: "),
        ("colour=red", "colour: unknown field"),
    ],
    ids=["choice", "duration", "unknown"],
)
def test_a_bad_set_stops_run_and_check_with_the_message_of_the_project_file_key(
    factory: Harness, setting: str, message: str
) -> None:
    spec = pair(factory)

    checked = factory.cli("check", "--project", str(factory.project_file), "--set", setting)
    ran = factory.run(spec, "--set", setting)

    prefix = f"factory: {display_path(factory.project_file)}: {message}"
    for result in (checked, ran):
        assert result.code == 2, result.out
        assert prefix in result.err
    assert "started" not in ran.out


def test_set_parses_yaml_and_needs_a_key_and_a_value(factory: Harness) -> None:
    factory.project["ci"] = {"timeout": "1h"}

    off = factory.cli("check", "--project", str(factory.project_file), "--set", "ci=off", "--set", "prevent_sleep=no")
    bare = factory.cli("check", "--project", str(factory.project_file), "--set", "landing")
    broken = factory.cli("check", "--project", str(factory.project_file), "--set", "caches=[.venv")

    assert off.code == 0, off.err
    assert bare.code == broken.code == 2
    assert "argument --set: must be key=value" in bare.err
    assert "argument --set: [.venv is not valid YAML" in broken.err
