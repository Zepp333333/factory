"""The contract of the Executor port, which every Executor kind of `wiring` and of the harness passes."""

import json
from pathlib import Path

import pytest

from factory.adapters.local_sandbox import LocalSandbox
from factory.pipeline.pipeline import AgentSettings
from factory.ports.executor import EVENTS, EVENTS_VERSION, AttemptRequest, AttemptResult, Executor, read_event
from factory.ports.sandbox import Needs
from factory.wiring import REGISTRIES
from harness import AgentScript, wait_until
from hosts import Host, Picked, host, kinds, made_kind  # noqa: F401

# The host CLI each kind runs its agent with, which `hosts` fakes.
FAKED = {"claude-code": "claude"}
STAGE = "implement"
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


@pytest.fixture(params=kinds(REGISTRIES.executors, harness=True), ids=str)
def executor(request: pytest.FixtureRequest, host: Host) -> Executor:  # noqa: F811
    made: Executor = made_kind(request.param, host, "executors", FAKED)
    return made


def started(executor: Executor, host: Host, script: AgentScript) -> tuple[AttemptRequest, LocalSandbox]:  # noqa: F811
    """Starts an Attempt of `script` in a LocalSandbox on a Workspace of its own, with a kit the Executor assembled."""
    host.play(script, STAGE)
    root = host.factory.root / "attempt"
    workspace, attempt_dir = root / "workspace", root / "attempt"
    workspace.mkdir(parents=True)
    attempt_dir.mkdir()
    (root / "prompt.md").write_text("Implement the Ticket.\n")
    (root / "brief.json").write_text(json.dumps({"ticket": "t1"}))
    (root / "schema.json").write_text(json.dumps(SCHEMA))
    executor.assemble(root / "skills", root / "kit")
    sandbox = LocalSandbox()
    sandbox.require(executor.needs())
    sandbox.create("s1", workspace, [])
    request = AttemptRequest(
        stage=STAGE,
        settings=AgentSettings(model=executor.default_model, executor=executor.name),
        prompt=root / "prompt.md",
        brief=root / "brief.json",
        output_schema=root / "schema.json",
        attempt_dir=attempt_dir,
        stream=attempt_dir / "stream.ndjson",
        events=attempt_dir / EVENTS,
        sandbox="s1",
        workspace=workspace,
        agent_home=root / "agent",
        kit=root / "kit",
    )
    return request, sandbox


def result_of(executor: Executor, host: Host, script: AgentScript) -> AttemptResult:  # noqa: F811
    request, sandbox = started(executor, host, script)
    handle = executor.start(request, sandbox)
    assert wait_until(lambda: handle.poll() is not None, 10), "the Attempt gave no result"
    result = handle.poll()
    assert result is not None
    return result


def test_an_attempt_polls_to_the_agents_output_and_what_it_spent(executor: Executor, host: Host) -> None:  # noqa: F811
    script = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt"}, turns=3, cost=0.5)

    result = result_of(executor, host, script)

    assert (result.failure, result.output) == (None, {"summary": "Add hello.txt"})
    assert result.usage is not None
    assert (result.usage.turns, result.usage.cost_usd) == (3, 0.5)
    assert (host.factory.root / "attempt" / "workspace" / "hello.txt").read_text() == "hello\n"


def test_an_attempt_writes_its_events_next_to_its_raw_stream(executor: Executor, host: Host) -> None:  # noqa: F811
    request, sandbox = started(executor, host, AgentScript(output={"summary": "Add hello.txt"}, turns=3, cost=0.5))
    handle = executor.start(request, sandbox)
    assert wait_until(lambda: handle.poll() is not None, 10), "the Attempt gave no result"

    events = [read_event(line) for line in request.events.read_bytes().splitlines()]

    assert request.stream.stat().st_size > 0
    assert all(event is not None and event["v"] == EVENTS_VERSION for event in events)
    types = [event["type"] for event in events if event is not None]
    assert "progress" in types
    assert types[-1] == "result"
    [usage] = [event for event in events if event is not None and event["type"] == "usage"]
    assert (usage["turns"], usage["cost_usd"]) == (3, 0.5)


@pytest.mark.parametrize(
    ("fail", "failure"),
    [("crash", "executor_crash"), ("auth", "auth"), ("rate_limited", "rate_limited")],
)
def test_a_failed_attempt_ends_with_its_failure_class(
    executor: Executor,
    host: Host,  # noqa: F811
    fail: str,
    failure: str,
) -> None:
    result = result_of(executor, host, AgentScript(fail=fail))  # type: ignore[arg-type]

    assert (result.failure, result.output) == (failure, None)
    assert result.reason


def test_kill_stops_an_agent_that_waits_and_returns_once_it_is_gone(executor: Executor, host: Host) -> None:  # noqa: F811
    request, sandbox = started(executor, host, AgentScript(wait_for=host.factory.root / "never"))
    handle = executor.start(request, sandbox)
    assert wait_until(lambda: request.stream.stat().st_size > 0, 10)

    handle.kill()

    result = handle.poll()
    assert result is not None
    assert result.output is None
    assert result.failure is not None


def test_needs_name_what_the_agent_asks_of_a_sandbox_and_merge_with_the_projects(executor: Executor) -> None:
    needs = executor.needs()

    merged = needs.merge(Needs(env={"TOOLCHAIN": "swift-6"}))

    assert merged.egress == needs.egress
    assert merged.env["TOOLCHAIN"] == "swift-6"
    assert {credential.env for credential in needs.credentials}.isdisjoint(needs.env)


def test_the_default_model_passes_the_settings_check_and_a_model_or_effort_of_no_harness_gets_a_hint(
    executor: Executor,
) -> None:
    assert executor.settings_problem(executor.default_model, None) is None
    assert executor.settings_problem("no such model", None)
    assert executor.settings_problem(executor.default_model, "no such effort")


def test_assemble_lets_a_library_skill_replace_the_kit_skill_of_its_name_and_the_kit_sha_follows_the_skills(
    executor: Executor, tmp_path: Path
) -> None:
    skills = tmp_path / "skills"
    (skills / "tdd").mkdir(parents=True)
    (skills / "tdd" / "SKILL.md").write_text("---\nname: tdd\ndescription: Ours\n---\nOur own loop.\n")
    plain = executor.kit_sha(tmp_path / "none")

    executor.assemble(skills, tmp_path / "kit")
    executor.assemble(skills, tmp_path / "kit")

    assert (tmp_path / "kit" / "skills" / "tdd" / "SKILL.md").read_text().endswith("Our own loop.\n")
    assert executor.kit_sha(skills) != plain
    assert executor.kit_sha(skills) == executor.kit_sha(skills)
