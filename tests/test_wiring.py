"""The registries of `wiring` pick every adapter by its kind name, and the project file names the kinds."""

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from factory.claude_code import ClaudeCodeExecutor
from factory.config import display_path
from factory.sandbox import Sandbox, SandboxKind
from factory.tracker import LocalTracker
from factory.wiring import EXECUTORS, SANDBOXES, TRACKERS, WORKBENCHES, Machine, TrackerKind, make_sandbox
from harness import AgentScript, Harness


@dataclass(frozen=True)
class FakeSettings:
    mode: str


TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"


def errors_of(factory: Harness, file: Path | None = None) -> list[str]:
    """Returns the error lines of `factory check` without the program prefix and the path of `file`."""
    result = factory.check()
    assert result.code != 0, result.out
    prefix = f"factory: {display_path(file or factory.project_file)}: "
    return [line.removeprefix(prefix) for line in result.err.splitlines()]


@pytest.fixture
def through_registries(factory: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """Makes the Machine of the factory harness pick its Executor, Sandbox and Workbench from the registries."""
    machine_of = factory.machine

    def machine(clock: object = None) -> Machine:
        return replace(machine_of(), executor=None, sandbox_for=make_sandbox, workbench=None)

    monkeypatch.setattr(factory, "machine", machine)
    return factory


def test_check_rejects_an_unknown_executor_and_lists_the_known_ones(factory: Harness) -> None:
    factory.project["executor"] = "codex"

    assert errors_of(factory) == ["executor: must be one of claude-code"]


def test_check_rejects_an_unknown_executor_of_a_stage_and_of_defaults(factory: Harness) -> None:
    factory.pipeline["defaults"]["executor"] = "codex"
    factory.pipeline["stages"]["implement"]["executor"] = "codex"

    assert errors_of(factory, factory.pipeline_file) == [
        "defaults.executor: must be one of claude-code",
        "stages.implement.executor: must be one of claude-code",
    ]


def test_check_rejects_an_unknown_sandbox_kind_and_workbench_and_lists_the_known_ones(factory: Harness) -> None:
    factory.project["sandbox"] = {"kind": "docker"}
    factory.project["workbench"] = "tmux"

    assert errors_of(factory) == [
        "sandbox.kind: must be local or sbx",
        "workbench: must be one of terminal, orca",
    ]


def test_claude_code_named_as_the_executor_runs_like_no_executor(through_registries: Harness) -> None:
    factory = through_registries
    factory.project["executor"] = "claude-code"
    named = factory.check()
    del factory.project["executor"]
    unnamed = factory.check()

    assert (named.code, named.out, named.err) == (unnamed.code, unnamed.out, unnamed.err) == (0, named.out, "")
    assert isinstance(EXECUTORS["claude-code"](), ClaudeCodeExecutor)


def test_a_fake_kind_of_every_port_runs_through_check_and_run(
    through_registries: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = through_registries
    for registry in (EXECUTORS, SANDBOXES, WORKBENCHES, TRACKERS):
        monkeypatch.setattr(registry, "_factories", dict(registry._factories))
    EXECUTORS.register("fake", lambda: factory.executor)
    made: list[object] = []

    def make_fake(settings: object) -> Sandbox:
        made.append(settings)
        return factory.sandbox

    SANDBOXES.register("fake", SandboxKind(("mode",), lambda body, report: FakeSettings(body["mode"]), make_fake))
    WORKBENCHES.register("fake", lambda: factory.workbench)
    TRACKERS.register(
        "fake", TrackerKind(lambda spec, tickets, machine: LocalTracker(Path(spec.removeprefix("fake:"))), _fake_link)
    )
    factory.project.update({"executor": "fake", "workbench": "fake", "sandbox": {"kind": "fake", "mode": "quick"}})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})

    assert factory.check().code == 0
    result = factory.run(f"fake:{spec}")

    assert result.code == 0, result.err
    assert len(factory.executor.requests) == 1
    assert FakeSettings("quick") in made


def _fake_link(spec: str) -> bool:
    return spec.startswith("fake:")


def test_check_names_the_kind_that_a_field_applies_to(factory: Harness) -> None:
    factory.project["sandbox"] = {"kind": "local", "template": "x", "cpus": 2}

    assert errors_of(factory) == [
        "sandbox.cpus: applies only to kind sbx",
        "sandbox.template: applies only to kind sbx",
    ]


def test_check_rejects_egress_on_a_kind_that_cannot_enforce_it(
    through_registries: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = through_registries
    monkeypatch.setattr(SANDBOXES, "_factories", dict(SANDBOXES._factories))
    SANDBOXES.register(
        "open", SandboxKind(("egress",), lambda body, report: FakeSettings("open"), lambda settings: factory.sandbox)
    )
    factory.project["sandbox"] = {"kind": "open", "egress": ["pypi.org"]}

    assert errors_of(factory) == ["sandbox.egress: cannot be enforced by kind open"]
