"""The registries of `wiring` pick every adapter by its kind name, and the project file names the kinds."""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from factory.adapters.claude_code import ClaudeCodeExecutor
from factory.adapters.local_tracker import LocalTracker
from factory.config import display_path, load_project
from factory.ports.executor import AgentKit
from factory.ports.kind import CheckContext, Kind, Problems, Report, Tools
from factory.ports.sandbox import Sandbox
from factory.ports.tracker import SpecLink, Tracker
from factory.wiring import AGENT_KIT, REGISTRIES, Machine, made
from fake_kinds import CodeHost
from harness import AgentScript, Harness, ScriptedExecutor, run_id_of
from test_file_issues import files_issues


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


def parse_mode(body: dict[str, Any], report: Report) -> FakeSettings:
    return FakeSettings(body.get("mode", "slow"))


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


def test_check_rejects_an_unknown_kind_in_the_block_of_every_port(factory: Harness) -> None:
    factory.project.update(
        {"executor": {"kind": "codex"}, "workbench": {"kind": "tmux"}, "code_host": {"kind": "bitbucket"}}
    )

    assert errors_of(factory) == [
        "factory.yaml: workbench.kind: must be terminal or orca",
        "factory.yaml: executor.kind: must be claude-code",
        "factory.yaml: code_host.kind: must be github or gitlab",
    ]


def test_check_accepts_a_stage_executor_other_than_the_projects_and_rejects_sandbox_fields_its_kind_cannot_take(
    factory: Harness,
) -> None:
    factory.registries.executors.register("fake", Kind(lambda settings, tools: factory.executor))
    factory.registries.sandboxes.register(
        "open", Kind(lambda settings, tools: factory.sandbox, ("egress",), lambda body, report: FakeSettings("open"))
    )
    factory.project.update(
        {"executor": "fake", "sandbox": {"kind": "open", "egress": ["pypi.org"], "template": "x", "cpus": 2}}
    )
    factory.pipeline["stages"]["implement"]["executor"] = "claude-code"

    assert errors_of(factory) == [
        "factory.yaml: sandbox.cpus: applies only to kind sbx",
        "factory.yaml: sandbox.template: applies only to kind sbx",
        "factory.yaml: sandbox.egress: cannot be enforced by kind open",
    ]


def test_a_field_of_one_kind_under_another_applies_only_to_its_kind_in_every_port(factory: Harness) -> None:
    fields = Kind[Any](lambda settings, tools: None, ("mode",), parse_mode)
    factory.registries.executors.register("fake", fields)
    factory.registries.workbenches.register("fake", fields)
    factory.registries.pull_requests.register("fake", fields)
    factory.project.update(
        {
            "executor": {"kind": "claude-code", "mode": "quick"},
            "workbench": {"kind": "terminal", "mode": "quick"},
            "code_host": {"kind": "github", "mode": "quick"},
            "sandbox": {"kind": "local", "mode": "quick"},
        }
    )

    assert errors_of(factory) == [
        "factory.yaml: sandbox.mode: unknown field",
        "factory.yaml: workbench.mode: applies only to kind fake",
        "factory.yaml: executor.mode: applies only to kind fake",
        "factory.yaml: code_host.mode: applies only to kind fake",
    ]


@pytest.mark.parametrize(
    ("port", "kind"),
    [("executor", "claude-code"), ("sandbox", "local"), ("workbench", "orca"), ("code_host", "gitlab")],
)
def test_the_block_and_the_scalar_form_of_a_kind_load_the_same_project(factory: Harness, port: str, kind: str) -> None:
    machine = factory.machine()
    factory.project[port] = kind
    factory.machine()
    scalar = load_project(factory.project_file, machine.library, machine.kinds)
    factory.project[port] = {"kind": kind}
    factory.machine()
    block = load_project(factory.project_file, machine.library, machine.kinds)

    assert getattr(block, port) == getattr(scalar, port)
    assert getattr(block, port).kind == kind


def test_a_run_with_the_block_form_of_every_port_runs_as_one_with_the_scalar_form(factory: Harness) -> None:
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.project.update({"executor": "claude-code", "workbench": "terminal"})
    scalar = factory.run(spec)
    factory.project.update(
        {"executor": {"kind": "claude-code"}, "sandbox": {"kind": "local"}, "workbench": {"kind": "terminal"}}
    )
    block = factory.run(spec)

    assert (scalar.code, block.code) == (0, 0), scalar.err + block.err
    assert [request.stage for request in factory.executor.requests] == ["implement", "implement"]
    [scalar_started], [block_started] = (factory.events(run_id, "RunStarted") for run_id in factory.run_ids())
    assert (scalar_started["settings"]["executor"], block_started["settings"]["executor"]) == (
        "claude-code",
        {"kind": "claude-code"},
    )


def test_the_errors_of_a_kinds_check_stop_check_and_run_and_its_warnings_are_shown(factory: Harness) -> None:
    contexts: list[CheckContext] = []

    def check(settings: FakeSettings, context: CheckContext) -> Problems:
        contexts.append(context)
        if settings.mode == "broken":
            return Problems(errors=("the fake Executor is broken",))
        return Problems(warnings=(f"the fake Executor runs {settings.mode}",))

    factory.registries.executors.register(
        "fake", Kind(lambda settings, tools: factory.executor, ("mode",), parse_mode, check)
    )
    factory.project["executor"] = {"kind": "fake", "mode": "broken"}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    assert factory.check().err.splitlines() == ["factory: the fake Executor is broken"]
    run = factory.run(spec)
    assert (run.code, run.err.splitlines()) == (2, ["factory: the fake Executor is broken"])

    factory.project["executor"] = {"kind": "fake"}
    checked = factory.check()

    assert (checked.code, checked.err) == (0, "factory: warning: the fake Executor runs slow\n")
    assert contexts[-1].repo == factory.repo
    assert contexts[-1].base == "main"


def test_claude_code_checks_like_no_executor_and_a_fake_kind_of_every_port_runs_through_check_and_run(
    factory: Harness,
) -> None:
    factory.project["executor"] = "claude-code"
    named = factory.check()
    del factory.project["executor"]
    unnamed = factory.check()

    assert (named.code, named.out, named.err) == (unnamed.code, unnamed.out, unnamed.err) == (0, named.out, "")
    assert isinstance(made(REGISTRIES.executors["claude-code"], Machine()), ClaudeCodeExecutor)

    registries = factory.registries
    registries.executors.register("fake", Kind(lambda settings, tools: factory.executor))
    made_with: list[object] = []

    def make_fake(settings: FakeSettings, tools: Tools) -> Sandbox:
        made_with.append(settings)
        return factory.sandbox

    registries.sandboxes.register("fake", Kind(make_fake, ("mode",), parse_mode))
    registries.workbenches.register("fake", Kind(lambda settings, tools: factory.workbench))
    registries.trackers.register("fake", Kind(make_fake_tracker, picks=fake_link))
    code_host = CodeHost(factory.remote)
    registries.pull_requests.register("fake", code_host.kind())
    factory.project.update(
        {
            "executor": "fake",
            "workbench": {"kind": "fake"},
            "sandbox": {"kind": "fake", "mode": "quick"},
            "code_host": "fake",
        }
    )
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.git("remote", "add", "origin", str(factory.remote))
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})

    assert factory.check().code == 0
    result = factory.run(f"fake:{spec}")

    assert result.code == 0, result.err
    assert len(factory.executor.requests) == 1
    assert FakeSettings("quick") in made_with
    run_id = run_id_of(result.out)
    [pull] = code_host.pulls
    assert (pull.head, pull.base) == (f"factory/{run_id}", "main")
    assert factory.remote_branches()[pull.head] == factory.git("rev-parse", pull.head)


class FolderExecutor(ScriptedExecutor):
    """FolderExecutor copies the Agent kit and the Run's skills into its folder as they are, beside the Claude Code kit.

    It keeps the Claude Code kit, because it plays its Attempts through the scripted `claude`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.assembled: list[tuple[AgentKit, Path]] = []

    def assemble(self, kit: AgentKit, skills: Path, into: Path) -> None:
        super().assemble(kit, skills, into)
        self.assembled.append((kit, skills))
        shutil.copytree(kit.root, into / "neutral")
        shutil.copytree(skills, into / "neutral" / "skills", dirs_exist_ok=True)


def test_a_fake_executor_gets_the_agent_kit_and_the_runs_skills_in_its_folder(factory: Harness) -> None:
    fake = FolderExecutor()
    factory.registries.executors.register("fake", Kind(lambda settings, tools: fake))
    factory.project["executor"] = "fake"
    factory.pipeline["roles"]["implementer"]["skills"] = ["extra"]
    extra = factory.repo / ".factory" / "skills" / "extra" / "SKILL.md"
    extra.parent.mkdir(parents=True)
    extra.write_text("---\nname: extra\ndescription: Extra\n---\nDo more.\n")
    fake.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.err
    run_dir = factory.run_dir(run_id_of(result.out))
    kit = run_dir / "kit" / "fake" / "neutral"
    assert fake.assembled == [(AGENT_KIT, run_dir / "library" / "skills")]
    assert (kit / "instructions.md").read_text() == AGENT_KIT.instructions.read_text()
    assert (kit / "mcp.yaml").exists()
    assert sorted(path.parent.name for path in kit.glob("skills/*/SKILL.md")) == sorted([*AGENT_KIT.skills, "extra"])
    assert (kit / "skills" / "extra" / "SKILL.md").read_text().endswith("Do more.\n")
    assert [request.kit for request in fake.requests] == [kit.parent]


def make_fake_tracker(link: SpecLink, tools: Tools) -> Tracker:
    local = LocalTracker(Path(link.spec.removeprefix("fake:")))
    return Tracker(local, local, local, local)


def fake_link(spec: str) -> bool:
    return spec.startswith("fake:")


def make_reader(link: SpecLink, tools: Tools) -> Tracker:
    """Makes a Tracker that only reads the Spec and writes the PR text, as a read-only Tracker would."""
    local = LocalTracker(Path(link.spec.removeprefix("fake:")))
    return Tracker(spec=local, closing=local)


def test_a_tracker_with_only_a_spec_source_and_closing_runs_and_check_names_the_role_file_issues_needs(
    factory: Harness,
) -> None:
    factory.registries.trackers.register("fake", Kind(make_reader, picks=fake_link))
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})
    factory.executor.scripts["decide"] = AgentScript(output={"outcome": "approve", "reasons": ["fine"]})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(f"fake:{spec}")

    assert result.code == 0, result.err
    assert not (spec / "projection").exists()

    files_issues(factory)
    checked = factory.cli("check", f"fake:{spec}", "--project", str(factory.project_file))

    assert (checked.code, checked.err) == (
        2,
        f"factory: the Operation file_issues files issues through the role IssueDesk,"
        f" which the Tracker of fake:{spec} does not serve\n",
    )
