"""The Attempt renderer that OrcaWorkbench starts fills the agent card of its pane through Orca's hook server.

These tests run the renderer through its CLI on a fixture stream against a fake hook server on loopback.
"""

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from factory.adapters.claude_code import Normalizer
from factory.adapters.terminal_workbench import OrcaWorkbench
from factory.ports.executor import EVENTS, attempt_event, card_fact, event_line
from fake_hook_server import HookServer
from harness import AgentScript, Harness

FIXTURE = Path(__file__).parent / "fixtures" / "streams" / "card.ndjson"
TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
PROMPT = "Write hello.txt."
ANSI = re.compile(r"\x1b\[[0-9;]*m")
PANE = {"ORCA_PANE_KEY": "tab-1:0", "ORCA_TAB_ID": "tab-1", "ORCA_WORKTREE_ID": "worktree-1"}


@pytest.fixture
def server() -> Iterator[HookServer]:
    hook = HookServer()
    yield hook
    hook.stop()


def endpoint_file(path: Path, port: int, token: str = "token") -> Path:
    path.write_text(
        f"ORCA_AGENT_HOOK_PORT={port}\nORCA_AGENT_HOOK_TOKEN={token}\n"
        "ORCA_AGENT_HOOK_ENV=production\nORCA_AGENT_HOOK_VERSION=1\nORCA_AGENT_HOOK_TRANSPORT=form\n"
    )
    return path


def pane_env(endpoint: Path) -> dict[str, str]:
    return {**PANE, "ORCA_AGENT_HOOK_ENDPOINT": str(endpoint)}


def events_of_fixture(tmp_path: Path, stream: Path = FIXTURE) -> Path:
    """Writes the Attempt events that the Claude Code Executor makes of `stream`, and returns their file."""
    normalizer = Normalizer(prompt=PROMPT)
    events = tmp_path / EVENTS
    lines = stream.read_bytes().splitlines()
    events.write_bytes(b"".join(event_line(e) for line in lines for e in normalizer.line(line)))
    return events


def render(events: Path, env: dict[str, str], *flags: str) -> str:
    """Runs the renderer through its CLI in a pane with `env` and returns what it prints."""
    clean = {name: value for name, value in os.environ.items() if not name.startswith("ORCA_")}
    command = [sys.executable, "-m", "factory.render", str(events), *flags]
    return subprocess.run(command, env={**clean, **env}, check=True, capture_output=True, text=True, timeout=30).stdout


def payloads(server: HookServer) -> list[dict[str, Any]]:
    return [json.loads(post.form["payload"]) for post in server.posts]


def test_the_renderer_with_the_orca_card_posts_the_events_of_its_stream_to_the_hook_server_of_its_pane(
    tmp_path: Path, server: HookServer
) -> None:
    events = events_of_fixture(tmp_path)

    render(events, pane_env(endpoint_file(tmp_path / "endpoint.env", server.port)), "--card", "orca")

    session = {"session_id": "s-card"}
    bash = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}, "tool_use_id": "t1"}
    task = {
        "tool_name": "Task",
        "tool_input": {"subagent_type": "Explore", "prompt": "Look around."},
        "tool_use_id": "a1",
    }
    subagent = {"agent_id": "a1", "agent_type": "Explore"}
    assert payloads(server) == [
        {"hook_event_name": "SessionStart", "source": "startup", **session},
        {"hook_event_name": "UserPromptSubmit", "prompt": PROMPT, **session},
        {"hook_event_name": "PreToolUse", **bash, **session},
        {"hook_event_name": "PostToolUse", **bash, "tool_response": "README.md", **session},
        {"hook_event_name": "PreToolUse", **task, **session},
        {"hook_event_name": "SubagentStart", **subagent, **session},
        {"hook_event_name": "SubagentStop", **subagent, **session},
        {"hook_event_name": "PostToolUse", **task, "tool_response": "Found it.", **session},
        {"hook_event_name": "Stop", "last_assistant_message": "All done.", **session},
    ]
    assert {post.path for post in server.posts} == {"/hook/claude"}
    envelope = {key: value for key, value in server.posts[0].form.items() if key != "payload"}
    assert envelope == {
        "paneKey": "tab-1:0",
        "tabId": "tab-1",
        "worktreeId": "worktree-1",
        "env": "production",
        "version": "1",
    }


@pytest.mark.parametrize(
    ("lines", "reply"),
    [(7, "Done."), (5, "I will list the files.")],
    ids=["after a reply", "where a subagent spoke last"],
)
def test_the_renderer_ends_the_turn_with_the_main_agents_last_reply_when_the_agent_ends_without_a_result(
    tmp_path: Path, server: HookServer, lines: int, reply: str
) -> None:
    stream = tmp_path / "stream.ndjson"
    stream.write_bytes(b"".join(FIXTURE.read_bytes().splitlines(keepends=True)[:lines]))
    events = events_of_fixture(tmp_path, stream)
    log = tmp_path / "terminal.log"
    (tmp_path / "terminal.log.stop").touch()

    render(events, pane_env(endpoint_file(tmp_path / "endpoint.env", server.port)), "--card", "orca", "--log", str(log))

    assert payloads(server)[-1] == {
        "hook_event_name": "Stop",
        "last_assistant_message": reply,
        "session_id": "s-card",
    }
    assert [line for line in ANSI.sub("", log.read_text()).splitlines() if "card connected" in line] == [
        f"Orca card connected to the hook server on port {server.port}"
    ]


def test_a_renderer_without_the_card_posts_nothing_and_says_nothing_of_it(tmp_path: Path, server: HookServer) -> None:
    events = events_of_fixture(tmp_path)

    out = render(events, pane_env(endpoint_file(tmp_path / "endpoint.env", server.port)))

    assert server.posts == []
    assert "card" not in out


@pytest.mark.parametrize(
    ("gone", "why"),
    [
        ("ORCA_PANE_KEY", "ORCA_PANE_KEY"),
        ("ORCA_TAB_ID", "ORCA_TAB_ID"),
        ("ORCA_WORKTREE_ID", "ORCA_WORKTREE_ID"),
        ("ORCA_AGENT_HOOK_ENDPOINT", "ORCA_AGENT_HOOK_ENDPOINT"),
        ("the file", "endpoint file"),
        ("the token", "endpoint file"),
        ("the text", "endpoint file"),
    ],
)
def test_without_the_pane_variables_or_the_endpoint_file_the_renderer_posts_nothing_and_renders_as_now(
    tmp_path: Path, server: HookServer, gone: str, why: str
) -> None:
    events = events_of_fixture(tmp_path)
    endpoint = endpoint_file(tmp_path / "endpoint.env", server.port)
    env = {name: value for name, value in pane_env(endpoint).items() if name != gone}
    if gone == "the file":
        endpoint.unlink()
    if gone == "the token":
        endpoint.write_text(f"ORCA_AGENT_HOOK_PORT={server.port}\n")
    if gone == "the text":
        endpoint.write_bytes(b"\xff\n")
    plain = render(events, {})

    out = render(events, env, "--card", "orca")

    assert server.posts == []
    said = [line for line in out.splitlines() if "Orca card" in line]
    assert len(said) == 1 and "not connected" in said[0] and why in said[0]
    assert [line for line in out.splitlines() if "Orca card" not in line] == plain.splitlines()


def test_the_renderer_rereads_the_endpoint_file_after_a_403_and_loses_no_event(
    tmp_path: Path, server: HookServer
) -> None:
    server.token = "minted-on-restart"
    endpoint = endpoint_file(tmp_path / "endpoint.env", server.port, token="stale")

    def orca_restarted() -> None:
        endpoint_file(endpoint, server.port, token="minted-on-restart")

    server.on_forbidden = orca_restarted
    events = events_of_fixture(tmp_path)

    out = render(events, pane_env(endpoint), "--card", "orca")

    assert [payload["hook_event_name"] for payload in payloads(server)] == [
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PreToolUse",
        "SubagentStart",
        "SubagentStop",
        "PostToolUse",
        "Stop",
    ]
    assert "Orca card connected" in out


def test_a_token_the_endpoint_file_does_not_renew_leaves_the_card_not_connected(
    tmp_path: Path, server: HookServer
) -> None:
    server.token = "another"

    out = render(
        events_of_fixture(tmp_path), pane_env(endpoint_file(tmp_path / "endpoint.env", server.port)), "--card", "orca"
    )

    assert server.posts == []
    assert "Orca card not connected: the hook server refused the token" in out


def test_an_agent_that_starts_no_session_posts_nothing_and_the_log_says_so(tmp_path: Path, server: HookServer) -> None:
    events = tmp_path / EVENTS
    events.write_bytes(
        event_line(attempt_event("card", fact="session", session_id=None))
        + event_line(attempt_event("card", fact="prompt", text=PROMPT))
        + event_line(attempt_event("result", status="x"))
    )

    out = render(events, pane_env(endpoint_file(tmp_path / "endpoint.env", server.port)), "--card", "orca")

    assert server.posts == []
    assert "Orca card not connected: the agent started no session" in out


@pytest.mark.parametrize("trouble", ["hanging", "failing", "absent"])
def test_a_slow_absent_or_failing_hook_server_does_not_slow_down_or_break_the_rendering(
    tmp_path: Path, server: HookServer, trouble: str
) -> None:
    events = events_of_fixture(tmp_path)
    # So many facts that a renderer that waits for each post runs into the timeout of `render`.
    many = attempt_event("card", fact="tool", name="Bash", input={}, id="many")
    lines = events.read_bytes().splitlines(keepends=True)
    events.write_bytes(b"".join(lines[:3]) + event_line(many) * 200 + b"".join(lines[3:]))
    endpoint = endpoint_file(tmp_path / "endpoint.env", server.port)
    if trouble == "hanging":
        server.hangs = True
    elif trouble == "failing":
        server.status = 500
    else:
        endpoint_file(endpoint, port=1)
    plain = render(events, {})

    out = render(events, pane_env(endpoint), "--card", "orca")

    said = [line for line in out.splitlines() if "Orca card" in line]
    assert len(said) == 1 and "not connected" in said[0]
    assert [line for line in out.splitlines() if "Orca card" not in line] == plain.splitlines()


def test_a_hook_server_too_slow_for_all_events_still_gets_the_stop(tmp_path: Path, server: HookServer) -> None:
    server.answer_seconds = 0.7

    render(
        events_of_fixture(tmp_path), pane_env(endpoint_file(tmp_path / "endpoint.env", server.port)), "--card", "orca"
    )

    assert payloads(server)[-1] == {
        "hook_event_name": "Stop",
        "last_assistant_message": "All done.",
        "session_id": "s-card",
    }


def test_an_agent_that_repeats_its_init_event_starts_the_session_once(tmp_path: Path, server: HookServer) -> None:
    events = tmp_path / EVENTS
    session = event_line(attempt_event("card", fact="session", session_id="s-card"))
    events.write_bytes(session + session + event_line(attempt_event("result", status="x")))

    render(events, pane_env(endpoint_file(tmp_path / "endpoint.env", server.port)), "--card", "orca")

    assert payloads(server) == [{"hook_event_name": "SessionStart", "source": "startup", "session_id": "s-card"}]


def test_a_subagent_call_that_names_no_type_gives_no_agent_type() -> None:
    block = {"type": "tool_use", "id": "a1", "name": "Task", "input": {"prompt": "Look around."}}
    line = {"type": "assistant", "parent_tool_use_id": None, "message": {"content": [block]}}

    facts = Normalizer().line(json.dumps(line).encode())

    assert card_fact("subagent_start", id="a1", agent_type=None) in facts


def test_the_renderer_refuses_a_card_of_an_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render(events_of_fixture(tmp_path), {}, "--card", "unknown")

    assert refused.value.returncode == 2
    assert "invalid choice" in refused.value.stderr


def test_a_post_carries_no_agent_process_and_no_transcript_path(tmp_path: Path, server: HookServer) -> None:
    render(
        events_of_fixture(tmp_path), pane_env(endpoint_file(tmp_path / "endpoint.env", server.port)), "--card", "orca"
    )

    assert server.posts
    for post in server.posts:
        assert "agentProcess" not in post.form
        assert "transcript_path" not in post.form["payload"]
        assert "SessionEnd" not in post.form["payload"]


@pytest.mark.parametrize("orca", [True, False], ids=["orca", "terminal"])
def test_a_run_posts_the_prompt_of_the_attempt_only_through_the_renderer_orca_starts(
    factory: Harness,
    tmp_path: Path,
    server: HookServer,
    monkeypatch: pytest.MonkeyPatch,
    orca: bool,
) -> None:
    for name, value in pane_env(endpoint_file(tmp_path / "endpoint.env", server.port)).items():
        monkeypatch.setenv(name, value)
    if orca:
        factory.workbench_override = OrcaWorkbench(command=factory.orca.command)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "wrote hello.txt"}
    )

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    if not orca:
        assert server.posts == []
        return
    names = [payload["hook_event_name"] for payload in payloads(server)]
    assert names[:2] == ["SessionStart", "UserPromptSubmit"]
    assert names[-1] == "Stop"
    prompt = payloads(server)[1]["prompt"]
    assert f"Your Brief is {factory.executor.requests[0].brief}." in prompt
    assert all(payload["session_id"] for payload in payloads(server))
