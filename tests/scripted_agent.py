"""Scripted agent that stands in for the `claude` CLI and plays one Attempt from a scenario file.

It takes the scenario path first and the `claude` arguments after it, and records both with its
environment and stdin in `launch.json` next to the scenario.
It writes the scenario's files into the current directory, which is the Workspace,
and prints an NDJSON stream shaped like `claude -p --output-format stream-json`.
Its init event lists what the real CLI would load for these arguments, plus the scenario's `leak`.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# These are what Claude Code 2.1.306 loads on its own under the factory's launch flags.
BUILTIN_PLUGINS = ["cc-plugin-agents-md", "cc-plugin-telemetry", "cc-plugin-plugin-authoring"]
BUILTIN_SKILLS = ["design", "doctor", "plugin-authoring"]
BUILTIN_AGENTS = ["claude", "Explore", "general-purpose", "Plan", "statusline-setup"]

RECORDED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "GH_TOKEN",
    "UV_PROJECT_ENVIRONMENT",
)


def emit(event: dict[str, Any]) -> None:
    print(json.dumps(event), flush=True)


def flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def init_event(argv: list[str], leak: dict[str, list[str]]) -> dict[str, Any]:
    plugins = [{"name": name, "path": "builtin", "source": f"{name}@builtin"} for name in BUILTIN_PLUGINS]
    skills, agents = list(BUILTIN_SKILLS), list(BUILTIN_AGENTS)
    plugin_dir = flag(argv, "--plugin-dir")
    if plugin_dir is not None:
        root = Path(plugin_dir)
        name = json.loads((root / ".claude-plugin" / "plugin.json").read_text())["name"]
        plugins.insert(0, {"name": name, "path": str(root), "source": f"{name}@inline"})
        skills += [f"{name}:{path.parent.name}" for path in sorted(root.glob("skills/*/SKILL.md"))]
        agents += [f"{name}:{path.stem}" for path in sorted(root.glob("agents/*.md"))]
    if "project" in (flag(argv, "--setting-sources") or "").split(","):
        skills = [path.parent.name for path in sorted(Path(".claude/skills").glob("*/SKILL.md"))] + skills
    plugins += [{"name": source, "path": "elsewhere", "source": source} for source in leak.get("plugins", [])]
    return {
        "type": "system",
        "subtype": "init",
        "model": flag(argv, "--model"),
        "plugins": plugins,
        "skills": skills + leak.get("skills", []),
        "agents": agents + leak.get("agents", []),
        "mcp_servers": [{"name": name, "status": "connected"} for name in leak.get("mcp_servers", [])],
    }


def usage_fields(scenario: dict[str, Any]) -> dict[str, Any]:
    """Returns the turns, cost and tokens of the `result` event, with `modelUsage` split into a model and a subagent."""
    input, output, read, write = scenario.get("tokens") or [0, 0, 0, 0]
    main = {
        "inputTokens": input,
        "outputTokens": output,
        "cacheReadInputTokens": read,
        "cacheCreationInputTokens": write,
    }
    subagent = {"inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": 3, "cacheCreationInputTokens": 4}
    return {
        "num_turns": scenario.get("turns", 1),
        "total_cost_usd": scenario.get("cost", 0),
        "usage": main,
        "modelUsage": {"claude-main": main, "claude-subagent": subagent} if scenario.get("tokens") else {},
    }


def main() -> None:
    scenario_path = Path(sys.argv[1])
    argv = sys.argv[2:]
    scenario = json.loads(scenario_path.read_text())
    launch = {"argv": argv, "env": {k: os.environ.get(k) for k in RECORDED_ENV}, "stdin": sys.stdin.read()}
    scenario_path.with_name("launch.json").write_text(json.dumps(launch))
    for name in scenario["remove"]:
        path = Path(name)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    for name, content in scenario["files"].items():
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for command in scenario["run"]:
        subprocess.run(command, shell=True, check=True)
    fail = scenario.get("fail")
    if fail == "hang":
        # The child stands in for a tool the agent started, which must die with the agent.
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3); open('late.txt', 'w').write('x')"])

    # The harness waits for this event, so the files are on disk before the fake clock runs.
    emit(init_event(argv, scenario.get("leak", {})))
    if fail == "crash":
        print("Error: the agent crashed", file=sys.stderr)
        sys.exit(1)
    if scenario.get("heartbeat"):
        # The Claude CLI's heartbeat about a long command keeps coming even when the command hangs.
        while True:
            emit({"type": "system", "subtype": "task_progress"})
            time.sleep(0.01)
    if fail == "hang" or scenario.get("leak"):
        time.sleep(3600)
    while scenario.get("wait_for") and not Path(scenario["wait_for"]).exists():
        time.sleep(0.01)
    if fail == "auth":
        emit({"type": "assistant", "error": "authentication_failed", "message": {"content": []}})
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": None,
                "terminal_reason": "api_error",
                "result": "Not logged in · Please run /login",
            }
        )
        sys.exit(1)
    if fail == "rate_limited":
        info = {"status": "rejected", "rateLimitType": "five_hour"}
        if scenario.get("resets_at") is not None:
            info["resetsAt"] = scenario["resets_at"]
        emit({"type": "rate_limit_event", "rate_limit_info": info})
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 429,
                "terminal_reason": "api_error",
                "result": "You've hit your session limit",
            }
        )
        sys.exit(1)
    if fail == "max_turns":
        result = {"type": "result", "subtype": "error_max_turns", "is_error": True, "terminal_reason": "max_turns"}
        emit({**result, **usage_fields(scenario)})
        return
    emit({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}})
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}})
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": scenario["output"],
            **usage_fields(scenario),
        }
    )


if __name__ == "__main__":
    main()
