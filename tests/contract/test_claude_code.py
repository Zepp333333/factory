"""Seam 3 contract tests check that the real `claude` CLI behaves as the scripted agent assumes.

They run only with FACTORY_CONTRACT=1.
The tests without a login need no subscription, and the test with one needs CLAUDE_CODE_OAUTH_TOKEN
from `claude setup-token`.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from factory.claude_code import AgentSet, ClaudeCodeExecutor, Kit
from factory.executor import AttemptRequest, AttemptResult, Profile
from factory.sandbox import LocalSandbox

pytestmark = pytest.mark.skipif(os.environ.get("FACTORY_CONTRACT") != "1", reason="set FACTORY_CONTRACT=1")

SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
    "additionalProperties": False,
}


def attempt(root: Path, profile: Profile, prompt: str) -> tuple[AttemptResult, Path]:
    """Runs one Attempt of the real CLI on a Workspace that holds a project skill, and returns its result and stream."""
    workspace = root / "workspace"
    skill = workspace / ".claude" / "skills" / "build" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: build\ndescription: Build the project\n---\nRun make.\n")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    attempt_dir = root / "attempt"
    attempt_dir.mkdir()
    (root / "prompt.md").write_text(prompt)
    (root / "schema.json").write_text(json.dumps(SCHEMA))
    (root / "brief.json").write_text("{}")
    request = AttemptRequest(
        stage="contract",
        model="haiku",
        profile=profile,
        prompt=root / "prompt.md",
        brief=root / "brief.json",
        output_schema=root / "schema.json",
        attempt_dir=attempt_dir,
        stream=attempt_dir / "stream.ndjson",
        sandbox="contract",
        workspace=workspace,
        agent_home=root / "agent",
    )
    sandbox = LocalSandbox()
    sandbox.create("contract", workspace)
    handle = ClaudeCodeExecutor().start(request, sandbox)
    deadline = time.monotonic() + 300
    while (result := handle.poll()) is None:
        assert time.monotonic() < deadline, "the CLI gave no result in 300s"
        time.sleep(0.1)
    return result, request.stream


@pytest.mark.parametrize("profile", ["project", "hermetic"])
def test_cli_without_login_loads_only_the_factory_set_and_fails_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: Profile
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    result, stream = attempt(tmp_path, profile, "Answer with the word ok.")

    assert (result.failure, result.reason) == ("auth", "Not logged in · Please run /login")
    init = next(e for e in map(json.loads, stream.read_text().splitlines()) if e.get("subtype") == "init")
    kit = Kit()
    assert AgentSet.of_init(init) == kit.required(profile, tmp_path / "workspace") | kit.builtin


@pytest.mark.skipif("CLAUDE_CODE_OAUTH_TOKEN" not in os.environ, reason="needs CLAUDE_CODE_OAUTH_TOKEN")
def test_cli_on_the_subscription_returns_structured_output_and_rate_limit_events(tmp_path: Path) -> None:
    result, stream = attempt(tmp_path, "project", "Answer with the word ok.")

    assert result.failure is None, result.reason
    assert isinstance(result.output, dict) and isinstance(result.output["answer"], str)
    events = [json.loads(line) for line in stream.read_text().splitlines() if line.strip()]
    limits = [e["rate_limit_info"] for e in events if e.get("type") == "rate_limit_event"]
    assert limits and all(info.get("status") in ("allowed", "allowed_warning", "rejected") for info in limits)
