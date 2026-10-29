import json
from typing import Any

import pytest

from factory.claude_code import KIT, window_usages
from harness import IMPLEMENTER_SCHEMA, RESETS_AT, AgentScript, Harness, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
DONE = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def only_attempt(factory: Harness) -> dict[str, Any]:
    [request] = factory.executor.requests
    record: dict[str, Any] = json.loads((request.attempt_dir / "attempt.json").read_text())
    return record


def test_agent_runs_claude_with_the_factory_launch_flags(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = DONE

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [request] = factory.executor.requests
    launch = factory.executor.launch(request)
    argv = launch["argv"]
    assert "--bare" not in argv
    assert flag(argv, "--output-format") == "stream-json"
    assert "--verbose" in argv
    assert json.loads(flag(argv, "--json-schema")) == IMPLEMENTER_SCHEMA
    assert flag(argv, "--model") == "sonnet"
    assert flag(argv, "--setting-sources") == "project"
    assert flag(argv, "--plugin-dir") == str(KIT / "plugin")
    assert flag(argv, "--settings") == str(KIT / "settings.json")
    assert "--strict-mcp-config" in argv
    assert flag(argv, "--mcp-config") == str(KIT / "mcp.json")
    assert flag(argv, "--append-system-prompt-file") == str(KIT / "instructions.md")
    prompt = flag(argv, "-p")
    assert prompt.startswith("Implement the Ticket.\n")
    assert str(request.brief) in prompt
    assert launch["stdin"] == ""
    assert launch["env"]["CLAUDE_CONFIG_DIR"] == str(factory.run_dir(run_id) / "agent")
    assert launch["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_agent_environment_is_scrubbed_of_api_credentials(factory: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-owner")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example.com")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = DONE

    assert factory.run(spec).code == 0
    [request] = factory.executor.requests
    env = factory.executor.launch(request)["env"]
    assert (env["ANTHROPIC_API_KEY"], env["ANTHROPIC_AUTH_TOKEN"], env["ANTHROPIC_BASE_URL"]) == (None, None, None)


def test_hermetic_stage_loads_no_setting_sources(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["profile"] = "hermetic"
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = DONE

    assert factory.run(spec).code == 0
    [request] = factory.executor.requests
    assert flag(factory.executor.launch(request)["argv"], "--setting-sources") == ""


def test_project_stage_may_load_the_target_projects_skills(factory: Harness) -> None:
    skill = factory.repo / ".claude" / "skills" / "build" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: build\ndescription: Build the project\n---\nRun make.\n")
    factory.git("add", "-A")
    factory.git("commit", "-q", "-m", "add skill")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = DONE

    result = factory.run(spec)

    assert result.code == 0, result.out
    assert only_attempt(factory)["outcome"] == "done"


def test_attempt_fails_when_the_agent_loads_more_than_the_factory_set(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    leak = {"plugins": ["rtk@owner"], "skills": ["rtk"], "mcp_servers": ["asana"]}
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, leak=leak)

    result = factory.run(spec)

    assert result.code == 1
    record = only_attempt(factory)
    assert record["cause"] == "agent_config"
    assert record["reason"] == (
        "init event: unexpected plugin rtk@owner; unexpected skill rtk; unexpected MCP server asana"
    )
    assert "hello.txt" in record["files"]


def test_failed_login_is_an_auth_failure(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(fail="auth")

    assert factory.run(spec).code == 1
    record = only_attempt(factory)
    assert record["cause"] == "auth"
    assert record["reason"] == "Not logged in · Please run /login"


def test_rejected_rate_limit_ends_the_attempt_as_rate_limited(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [AgentScript(fail="rate_limited"), DONE]

    assert factory.run(spec).code == 0
    first = factory.executor.requests[0]
    record = json.loads((first.attempt_dir / "attempt.json").read_text())
    assert record["cause"] == "rate_limited"
    assert record["resets_at"] == RESETS_AT.isoformat()


def test_agent_that_crashes_without_a_result_fails_with_its_stderr(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(fail="crash")

    assert factory.run(spec).code == 1
    record = only_attempt(factory)
    assert record["cause"] == "executor_crash"
    assert record["reason"] == "exited with code 1 and no result: Error: the agent crashed"


def test_error_result_is_an_executor_crash_with_its_subtype(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(fail="max_turns")

    assert factory.run(spec).code == 1
    record = only_attempt(factory)
    assert record["cause"] == "executor_crash"
    assert record["reason"] == "error_max_turns, terminal_reason max_turns, exit code 0"


def test_kit_settings_give_bash_a_ten_minute_timeout() -> None:
    env = json.loads((KIT / "settings.json").read_text())["env"]

    assert int(env["BASH_DEFAULT_TIMEOUT_MS"]) == 600_000
    assert int(env["BASH_MAX_TIMEOUT_MS"]) >= int(env["BASH_DEFAULT_TIMEOUT_MS"])


def test_executor_reads_the_usage_of_each_window_from_rate_limit_events() -> None:
    listed = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "unifiedWindows": {
                "five_hour": {"utilization": 0.57, "resetsAt": 1793121600},
                "seven_day": {"utilization": 0.2, "resetsAt": 1793268000},
                "broken": {"utilization": "high"},
            }
        },
    }
    single = {
        "type": "rate_limit_event",
        "rate_limit_info": {"rateLimitType": "five_hour", "utilization": 0.6, "resetsAt": 1793121600},
    }

    assert {k: (w.used, w.resets_at.timestamp()) for k, w in window_usages(listed).items()} == {
        "five_hour": (0.57, 1793121600),
        "seven_day": (0.2, 1793268000),
    }
    assert [w.used for w in window_usages(single).values()] == [0.6]
    assert window_usages({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}) == {}
    assert window_usages({"type": "assistant"}) == {}
