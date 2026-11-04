import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from factory.adapters.claude_code import KIT, AgentSet, ClaudeCodeExecutor, ClaudeCodeHandle, Kit, window_usages
from factory.ports.executor import EVENTS, STREAM, AgentKit, attempt_event, card_fact, read_event
from factory.ports.sandbox import MemoryReport
from factory.wiring import AGENT_KIT
from harness import IMPLEMENTER_SCHEMA, RESETS_AT, AgentScript, Harness, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
DONE = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def only_attempt(factory: Harness) -> dict[str, Any]:
    [request] = factory.executor.requests
    record: dict[str, Any] = json.loads((request.attempt_dir / "attempt.json").read_text())
    return record


def test_agent_runs_claude_with_the_factory_launch_flags_no_api_credentials_and_the_projects_skills(
    factory: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-owner")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.example.com")
    skill = factory.repo / ".claude" / "skills" / "build" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: build\ndescription: Build the project\n---\nRun make.\n")
    factory.git("add", "-A")
    factory.git("commit", "-q", "-m", "add skill")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = DONE

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert only_attempt(factory)["outcome"] == "done"
    [request] = factory.executor.requests
    launch = factory.executor.launch(request)
    argv = launch["argv"]
    assert "--bare" not in argv
    assert flag(argv, "--output-format") == "stream-json"
    assert "--verbose" in argv
    assert json.loads(flag(argv, "--json-schema")) == IMPLEMENTER_SCHEMA
    assert flag(argv, "--model") == "sonnet"
    assert flag(argv, "--setting-sources") == "project"
    kit = factory.run_dir(run_id) / "kit" / "claude-code"
    assert flag(argv, "--plugin-dir") == str(kit / "plugin")
    assert flag(argv, "--settings") == str(kit / "settings.json")
    assert (kit / "settings.json").read_text() == (KIT / "settings.json").read_text()
    assert "--strict-mcp-config" in argv
    assert flag(argv, "--mcp-config") == str(kit / "mcp.json")
    assert json.loads((kit / "mcp.json").read_text()) == {"mcpServers": {}}
    assert flag(argv, "--append-system-prompt-file") == str(kit / "instructions.md")
    assert (kit / "instructions.md").read_text() == AGENT_KIT.instructions.read_text()
    prompt = flag(argv, "-p")
    assert prompt.startswith("Implement the Ticket.\n")
    assert str(request.brief) in prompt
    assert launch["stdin"] == ""
    env = launch["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(factory.run_dir(run_id) / "agent")
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert (env["ANTHROPIC_API_KEY"], env["ANTHROPIC_AUTH_TOKEN"], env["ANTHROPIC_BASE_URL"]) == (None, None, None)


def test_each_failure_the_agent_streams_ends_its_attempt_with_its_cause_and_reason(factory: Harness) -> None:
    """A hermetic Stage's agent gets a rate limit, then fails its login, crashes, runs out of turns and leaks config."""
    factory.pipeline["stages"]["implement"]["profile"] = "hermetic"
    factory.pipeline["defaults"]["attempts"] = 4
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    leak = {"plugins": ["rtk@owner"], "skills": ["rtk"], "mcp_servers": ["asana"]}
    factory.executor.scripts["implement"] = [
        AgentScript(fail="rate_limited"),
        AgentScript(fail="auth"),
        AgentScript(fail="crash"),
        AgentScript(fail="max_turns"),
        AgentScript(files={"hello.txt": "hello\n"}, leak=leak),
    ]

    assert factory.run(spec).code == 1

    requests = factory.executor.requests
    assert [flag(factory.executor.launch(r)["argv"], "--setting-sources") for r in requests] == [""] * 5
    limited, auth, crash, turns, leaked = [json.loads((r.attempt_dir / "attempt.json").read_text()) for r in requests]
    assert (limited["cause"], limited["resets_at"]) == ("rate_limited", RESETS_AT.isoformat())
    assert (auth["cause"], auth["reason"]) == ("auth", "Not logged in · Please run /login")
    assert (crash["cause"], crash["reason"]) == (
        "executor_crash",
        "exited with code 1 and no result: Error: the agent crashed",
    )
    assert (turns["cause"], turns["reason"]) == (
        "executor_crash",
        "error_max_turns, terminal_reason max_turns, exit code 0",
    )
    assert leaked["cause"] == "agent_config"
    assert leaked["reason"] == (
        "init event: unexpected plugin rtk@owner; unexpected skill rtk; unexpected MCP server asana"
    )
    assert "hello.txt" in leaked["files"]


def test_assemble_translates_the_kits_mcp_servers_and_the_agent_must_load_them(tmp_path: Path) -> None:
    neutral = tmp_path / "neutral"
    (neutral / "skills" / "plan").mkdir(parents=True)
    (neutral / "skills" / "plan" / "SKILL.md").write_text("---\nname: plan\ndescription: Plan\n---\nPlan.\n")
    (neutral / "instructions.md").write_text("Be a Fresh agent.\n")
    servers = {"docs": {"url": "https://docs.example.com/mcp"}, "files": {"command": "files-mcp", "args": ["--ro"]}}
    (neutral / "mcp.yaml").write_text(yaml.safe_dump({"servers": servers}))

    ClaudeCodeExecutor().assemble(AgentKit(neutral), tmp_path / "none", tmp_path / "kit")

    assert json.loads((tmp_path / "kit" / "mcp.json").read_text()) == {
        "mcpServers": {
            "docs": {"type": "http", "url": "https://docs.example.com/mcp"},
            "files": {"command": "files-mcp", "args": ["--ro"]},
        }
    }
    assert (tmp_path / "kit" / "instructions.md").read_text() == "Be a Fresh agent.\n"
    required = Kit().required("hermetic", tmp_path / "workspace", tmp_path / "kit")
    assert required.mcp_servers == {"docs", "files"}
    assert required.skills == {"factory-kit:plan"}


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


def test_effort_is_passed_to_claude_from_defaults_and_stages_and_left_out_when_no_node_sets_it(
    factory: Harness,
) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = DONE

    assert factory.run(spec).code == 0
    [request] = factory.executor.requests
    assert "--effort" not in factory.executor.launch(request)["argv"]

    factory.executor.requests.clear()
    factory.pipeline["defaults"]["effort"] = "medium"
    assert factory.run(spec).code == 0
    [request] = factory.executor.requests
    assert flag(factory.executor.launch(request)["argv"], "--effort") == "medium"

    factory.executor.requests.clear()
    factory.pipeline["stages"]["implement"]["effort"] = "high"
    assert factory.run(spec).code == 0
    [request] = factory.executor.requests
    assert request.settings.effort == "high"
    assert flag(factory.executor.launch(request)["argv"], "--effort") == "high"


class Crashed:
    """Crashed is the process of an agent that has exited with code 1."""

    def poll(self) -> int | None:
        return 1

    def kill(self) -> None:
        pass

    def memory(self) -> MemoryReport | None:
        return None


def test_the_attempt_events_name_the_tool_of_each_result_and_keep_the_unfinished_line_of_a_crash(
    tmp_path: Path,
) -> None:
    call = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}
    denied = {"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True, "content": "denied"}
    stream, events = tmp_path / STREAM, tmp_path / EVENTS
    stream.write_text(
        json.dumps({"type": "assistant", "message": {"id": "msg_1", "content": [call]}})
        + "\n"
        + json.dumps({"type": "user", "message": {"content": [denied]}})
        + "\nSegmentation fa"
    )
    events.write_bytes(b"")
    handle = ClaudeCodeHandle(Crashed(), stream, tmp_path / "stderr.log", AgentSet(), AgentSet(), events)

    result = handle.poll()

    assert result is not None and result.failure == "executor_crash"
    assert [read_event(line) for line in events.read_bytes().splitlines()] == [
        attempt_event("progress"),
        attempt_event("tool", name="Bash", target='{"command": "ls"}', status="started"),
        card_fact("tool", id="toolu_1", name="Bash", input={"command": "ls"}),
        attempt_event("progress"),
        attempt_event("tool", name="Bash", status="failed", output="denied"),
        card_fact("tool_output", id="toolu_1", output="denied"),
        attempt_event("error", text="Segmentation fa"),
    ]
