"""The registries of `wiring` pick every adapter by its kind name, and the project file names the kinds."""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest

from factory import wiring
from factory.claude_code import ClaudeCodeExecutor
from factory.config import display_path
from factory.sandbox import Sandbox, SandboxKind
from factory.tracker import LocalTracker
from factory.wiring import Machine, Registry, TrackerKind, make_sandbox
from harness import AgentScript, Harness


@dataclass(frozen=True)
class FakeSettings:
    mode: str


TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"


def errors_of(factory: Harness) -> list[str]:
    """Returns the error lines of `factory check`, each with the name of its file in place of its path."""
    result = factory.check()
    assert result.code != 0, result.out
    files = {display_path(path): path.name for path in (factory.project_file, factory.pipeline_file)}
    errors = []
    for line in result.err.splitlines():
        path, message = line.removeprefix("factory: ").split(": ", 1)
        errors.append(f"{files[path]}: {message}")
    return errors


@pytest.fixture
def through_registries(factory: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """Makes the Machine of the factory harness pick its Executor, Sandbox and Workbench from the registries.

    Each registry is a copy, so a kind a test registers leaves with the test.
    """
    for name in ("EXECUTORS", "SANDBOXES", "WORKBENCHES", "TRACKERS"):
        copy: Registry[object] = Registry()
        for kind, made in getattr(wiring, name).items().items():
            copy.register(kind, made)
        monkeypatch.setattr(wiring, name, copy)
    machine_of = factory.machine

    def machine(clock: object = None) -> Machine:
        return replace(machine_of(), executor=None, sandbox_for=make_sandbox, workbench=None)

    monkeypatch.setattr(factory, "machine", machine)
    return factory


def test_check_rejects_an_unknown_kind_of_every_port_and_lists_the_known_ones(factory: Harness) -> None:
    factory.project.update({"executor": "codex", "sandbox": {"kind": "docker"}, "workbench": "tmux"})
    factory.pipeline["defaults"]["executor"] = "codex"
    factory.pipeline["stages"]["implement"]["executor"] = "codex"

    assert errors_of(factory) == [
        "factory.yaml: sandbox.kind: must be local or sbx",
        "factory.yaml: workbench: must be one of terminal, orca",
        "factory.yaml: executor: must be one of claude-code",
        "pipeline.yaml: defaults.executor: must be one of claude-code",
        "pipeline.yaml: stages.implement.executor: must be one of claude-code",
    ]


def test_check_rejects_a_stage_executor_other_than_the_projects_and_sandbox_fields_its_kind_cannot_take(
    through_registries: Harness,
) -> None:
    factory = through_registries
    wiring.EXECUTORS.register("fake", lambda: factory.executor)
    wiring.SANDBOXES.register(
        "open", SandboxKind(("egress",), lambda body, report: FakeSettings("open"), lambda settings: factory.sandbox)
    )
    factory.project.update(
        {"executor": "fake", "sandbox": {"kind": "open", "egress": ["pypi.org"], "template": "x", "cpus": 2}}
    )
    factory.pipeline["stages"]["implement"]["executor"] = "claude-code"

    assert errors_of(factory) == [
        "factory.yaml: sandbox.cpus: applies only to kind sbx",
        "factory.yaml: sandbox.template: applies only to kind sbx",
        "factory.yaml: sandbox.egress: cannot be enforced by kind open",
        "pipeline.yaml: stages.implement.executor: must be fake, the Executor of the project",
    ]


def test_claude_code_checks_like_no_executor_and_a_fake_kind_of_every_port_runs_through_check_and_run(
    through_registries: Harness,
) -> None:
    factory = through_registries
    factory.project["executor"] = "claude-code"
    named = factory.check()
    del factory.project["executor"]
    unnamed = factory.check()

    assert (named.code, named.out, named.err) == (unnamed.code, unnamed.out, unnamed.err) == (0, named.out, "")
    assert isinstance(wiring.EXECUTORS["claude-code"](), ClaudeCodeExecutor)

    wiring.EXECUTORS.register("fake", lambda: factory.executor)
    made: list[object] = []

    def make_fake(settings: object) -> Sandbox:
        made.append(settings)
        return factory.sandbox

    wiring.SANDBOXES.register(
        "fake", SandboxKind(("mode",), lambda body, report: FakeSettings(body["mode"]), make_fake)
    )
    wiring.WORKBENCHES.register("fake", lambda: factory.workbench)
    wiring.TRACKERS.register(
        "fake",
        TrackerKind(
            lambda spec, tickets, machine: LocalTracker(Path(spec.removeprefix("fake:"))),
            fake_link,
            folder=lambda tracker: cast(LocalTracker, tracker).pull_request_folder,
        ),
    )
    factory.project.update({"executor": "fake", "workbench": "fake", "sandbox": {"kind": "fake", "mode": "quick"}})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})

    assert factory.check().code == 0
    result = factory.run(f"fake:{spec}")

    assert result.code == 0, result.err
    assert len(factory.executor.requests) == 1
    assert FakeSettings("quick") in made


def fake_link(spec: str) -> bool:
    return spec.startswith("fake:")
