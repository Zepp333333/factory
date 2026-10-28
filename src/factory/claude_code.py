"""ClaudeCodeExecutor runs an unmodified `claude -p` on the owner's subscription with the factory's agent kit.

The agent never sees the owner's `~/.claude`.
It gets its own config directory per Run and only the kit's plugin, settings, MCP servers and instructions.
Its init event must list exactly that set, or the Attempt fails before the agent does any work.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory import front_matter
from factory.executor import AttemptRequest, AttemptResult, Profile, Usage
from factory.sandbox import Process, Sandbox, SandboxError

KIT = Path(__file__).with_name("kit")

# The first three would send the agent's requests past the subscription, and the agent never needs GitHub.
SCRUBBED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "GH_TOKEN")
SETTING_SOURCES: dict[Profile, str] = {"project": "project", "hermetic": ""}
AUTH_STATUSES = (401, 403)
STDERR_LINES = 20


@dataclass(frozen=True)
class AgentSet:
    """An AgentSet names the plugins, skills, agents and MCP servers the agent loads, as its init event lists them."""

    plugins: frozenset[str] = frozenset()
    skills: frozenset[str] = frozenset()
    agents: frozenset[str] = frozenset()
    mcp_servers: frozenset[str] = frozenset()

    @classmethod
    def of_init(cls, event: dict[str, Any]) -> "AgentSet":
        return cls(
            plugins=frozenset(str(p.get("source")) for p in event.get("plugins", [])),
            skills=frozenset(event.get("skills", [])),
            agents=frozenset(event.get("agents", [])),
            mcp_servers=frozenset(str(s.get("name")) for s in event.get("mcp_servers", [])),
        )

    def __or__(self, other: "AgentSet") -> "AgentSet":
        return AgentSet(*(getattr(self, f.name) | getattr(other, f.name) for f in fields(self)))


LABELS = {"plugins": "plugin", "skills": "skill", "agents": "agent", "mcp_servers": "MCP server"}


def mismatches(loaded: AgentSet, required: AgentSet, builtin: AgentSet) -> list[str]:
    """Lists what the agent loaded beyond `required` and `builtin`, and what of `required` it did not load."""
    found = []
    for f in fields(AgentSet):
        got, want, extra = getattr(loaded, f.name), getattr(required, f.name), getattr(builtin, f.name)
        found += [f"unexpected {LABELS[f.name]} {name}" for name in sorted(got - want - extra)]
        found += [f"missing {LABELS[f.name]} {name}" for name in sorted(want - got)]
    return found


class Kit:
    """A Kit is the factory's own agent config, which every agent gets in place of the owner's.

    `builtin.json` pins what the Claude Code CLI loads on its own and, in `absent_tools`, the tools
    the kit's settings keep from an agent. The contract tests check both.
    """

    def __init__(self, root: Path = KIT) -> None:
        self.root = root
        self.plugin = root / "plugin"
        self.settings = root / "settings.json"
        self.mcp = root / "mcp.json"
        self.instructions = root / "instructions.md"
        self.builtin = AgentSet(**{k: frozenset(v) for k, v in _json(root / "builtin.json").items() if k in LABELS})
        self.name = str(_json(self.plugin / ".claude-plugin" / "plugin.json")["name"])

    @property
    def skills(self) -> list[str]:
        """Lists the bare names of the kit's skills, which a Role's `skills` may name."""
        return _skills(self.plugin)

    def required(self, profile: Profile, workspace: Path) -> AgentSet:
        """Returns the set an agent must load: the kit's own, and the Target project's under `profile: project`."""
        kit = AgentSet(
            plugins=frozenset({f"{self.name}@inline"}),
            skills=frozenset(f"{self.name}:{name}" for name in _skills(self.plugin)),
            agents=frozenset(f"{self.name}:{name}" for name in _agents(self.plugin)),
            mcp_servers=frozenset(_json(self.mcp).get("mcpServers", {})),
        )
        if profile == "hermetic":
            return kit
        project = workspace / ".claude"
        return kit | AgentSet(skills=frozenset(_skills(project)), agents=frozenset(_agents(project)))


def _json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def _skills(root: Path) -> list[str]:
    return [_name(path, path.parent.name) for path in sorted(root.glob("skills/*/SKILL.md"))]


def _agents(root: Path) -> list[str]:
    return [_name(path, path.stem) for path in sorted(root.glob("agents/*.md"))]


def _name(path: Path, fallback: str) -> str:
    """Returns the `name` in the file's frontmatter, or `fallback` when it has none."""
    try:
        meta, _ = front_matter.split(path.read_text())
    except front_matter.FrontMatterError:
        return fallback
    name = meta.get("name")
    return name if isinstance(name, str) else fallback


class ClaudeCodeExecutor:
    """ClaudeCodeExecutor starts `command` in the Sandbox with the Kit's launch flags and a scrubbed environment."""

    def __init__(self, command: Sequence[str] = ("claude",), kit: Kit | None = None) -> None:
        self.command = tuple(command)
        self.kit = kit or Kit()

    def reads(self) -> list[Path]:
        return [self.kit.root]

    def start(self, request: AttemptRequest, sandbox: Sandbox) -> "ClaudeCodeHandle":
        request.agent_home.mkdir(parents=True, exist_ok=True)
        stderr = request.attempt_dir / "stderr.log"
        argv = [*self._env(request), *self.command, *self._flags(request)]
        process = sandbox.exec(request.sandbox, argv, stdout=request.stream, stderr=stderr)
        required = self.kit.required(request.profile, request.workspace)
        return ClaudeCodeHandle(process, request.stream, stderr, required, self.kit.builtin)

    def _env(self, request: AttemptRequest) -> list[str]:
        """Returns the `env` prefix that scrubs API credentials and points the agent at its own config."""
        unset = [arg for name in SCRUBBED_ENV for arg in ("-u", name)]
        return ["env", *unset, f"CLAUDE_CONFIG_DIR={request.agent_home}", "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1"]

    def _flags(self, request: AttemptRequest) -> list[str]:
        prompt = f"{request.prompt.read_text().rstrip()}\n\nYour Brief is {request.brief}.\n"
        return [
            "-p",
            prompt,
            "--model",
            request.model,
            "--output-format",
            "stream-json",
            "--verbose",
            "--json-schema",
            request.output_schema.read_text(),
            "--dangerously-skip-permissions",
            "--setting-sources",
            SETTING_SOURCES[request.profile],
            "--plugin-dir",
            str(self.kit.plugin),
            "--settings",
            str(self.kit.settings),
            "--strict-mcp-config",
            "--mcp-config",
            str(self.kit.mcp),
            "--append-system-prompt-file",
            str(self.kit.instructions),
        ]


class ClaudeCodeHandle:
    """A ClaudeCodeHandle reads the agent's stream as it grows.

    It kills the agent as soon as the init event shows a set other than the factory's.
    """

    def __init__(self, process: Process, stream: Path, stderr: Path, required: AgentSet, builtin: AgentSet) -> None:
        self._process = process
        self._stream = stream
        self._stderr = stderr
        self._required = required
        self._builtin = builtin
        self._offset = 0
        self._events: list[dict[str, Any]] = []
        self._checked = False
        self._refused: AttemptResult | None = None

    def poll(self) -> AttemptResult | None:
        # The exit code is taken before the read, so the read sees everything an exited agent wrote.
        try:
            code = self._process.poll()
        except SandboxError as error:
            return AttemptResult(output=None, failure="sandbox_lost", reason=str(error))
        self._read()
        init = next((e for e in self._events if _is_init(e)), None)
        if init is not None and not self._checked:
            self._checked = True
            wrong = mismatches(AgentSet.of_init(init), self._required, self._builtin)
            if wrong:
                self._process.kill()
                reason = "init event: " + "; ".join(wrong)
                self._refused = AttemptResult(output=None, failure="agent_config", reason=reason)
        if self._refused is not None:
            return self._refused
        if code is None:
            return None
        result = attempt_result(self._events, code, self._stderr_tail())
        if result.failure == "executor_crash":
            result = self._oom(result)
        if result.failure is None and init is None:
            return AttemptResult(output=None, failure="agent_config", reason="no init event")
        return result

    def kill(self) -> None:
        self._process.kill()

    def _oom(self, crash: AttemptResult) -> AttemptResult:
        """Tells a crash that the Sandbox's OOM killer caused from any other, by the Sandbox's own count."""
        memory = self._process.memory()
        if memory is None or memory.oom_kills == 0:
            return crash
        limit = f" with {memory.limit}" if memory.limit else ""
        reason = f"the OOM killer killed the agent in a Sandbox{limit}: {crash.reason}"
        return AttemptResult(output=None, failure="oom", reason=reason, memory=memory, usage=crash.usage)

    def _read(self) -> None:
        with self._stream.open("rb") as stream:
            stream.seek(self._offset)
            data = stream.read()
        complete = data.rpartition(b"\n")[0]
        if not complete:
            return
        self._offset += len(complete) + 1
        for line in complete.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                self._events.append(event)

    def _stderr_tail(self) -> str:
        if not self._stderr.is_file():
            return ""
        lines = self._stderr.read_text(errors="replace").strip().splitlines()
        return "\n".join(lines[-STDERR_LINES:])


def _is_init(event: dict[str, Any]) -> bool:
    return event.get("type") == "system" and event.get("subtype") == "init"


def attempt_result(events: list[dict[str, Any]], code: int, stderr: str) -> AttemptResult:
    """Returns how the Attempt ended from its exit code and stream, with what it spent when it gave a result."""
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    return replace(_outcome(events, result, code, stderr), usage=_usage(result))


def _usage(result: dict[str, Any] | None) -> Usage | None:
    """Returns what the `result` event says the Attempt spent, or None when there is no `result`.

    The tokens come from `modelUsage`, because `usage` counts only the main agent and leaves its subagents out.
    """
    if result is None:
        return None
    models = [m for m in (result.get("modelUsage") or {}).values() if isinstance(m, dict)]

    def tokens(name: str) -> int:
        return sum(int(m.get(name) or 0) for m in models)

    return Usage(
        cost_usd=float(result.get("total_cost_usd") or 0),
        turns=int(result.get("num_turns") or 0),
        input_tokens=tokens("inputTokens"),
        output_tokens=tokens("outputTokens"),
        cache_read_tokens=tokens("cacheReadInputTokens"),
        cache_write_tokens=tokens("cacheCreationInputTokens"),
    )


def _outcome(events: list[dict[str, Any]], result: dict[str, Any] | None, code: int, stderr: str) -> AttemptResult:
    """Returns how the Attempt ended from its exit code and stream.

    `subtype: success` alone proves nothing, because a failed login also ends with it and `is_error`.
    """
    if any(e.get("error") == "authentication_failed" for e in events) or (
        result is not None and result.get("api_error_status") in AUTH_STATUSES
    ):
        return AttemptResult(output=None, failure="auth", reason=_text(result) or "authentication failed")
    rejected = [
        e["rate_limit_info"]
        for e in events
        if e.get("type") == "rate_limit_event" and e.get("rate_limit_info", {}).get("status") == "rejected"
    ]
    if rejected:
        info = rejected[-1]
        resets_at = datetime.fromtimestamp(info["resetsAt"], UTC) if "resetsAt" in info else None
        reason = f"{info.get('rateLimitType', 'subscription')} limit rejected the agent"
        return AttemptResult(output=None, failure="rate_limited", reason=reason, resets_at=resets_at)
    if result is None:
        reason = f"exited with code {code} and no result" + (f": {stderr}" if stderr else "")
        return AttemptResult(output=None, failure="executor_crash", reason=reason)
    if code != 0 or result.get("is_error") or result.get("subtype") != "success":
        reason = f"{result.get('subtype')}, terminal_reason {result.get('terminal_reason')}, exit code {code}"
        text = _text(result)
        return AttemptResult(output=None, failure="executor_crash", reason=f"{reason}: {text}" if text else reason)
    output = result.get("structured_output")
    if not isinstance(output, dict):
        return AttemptResult(output=None, failure="executor_crash", reason="the result has no structured_output")
    return AttemptResult(output=output, failure=None)


def _text(result: dict[str, Any] | None) -> str:
    return str((result or {}).get("result") or "").strip().partition("\n")[0]
