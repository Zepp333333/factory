"""ClaudeCodeExecutor runs an unmodified `claude -p` on the owner's subscription with the factory's Agent kit.

The agent never sees the owner's `~/.claude`.
It gets its own config directory per Run and only the plugin, settings, MCP servers and instructions
that the Executor translates the Agent kit into.
Its init event must list exactly that set, or the Attempt fails before the agent does any work.
"""

import contextlib
import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from factory.core import front_matter
from factory.pipeline.fingerprint import tree_sha
from factory.pipeline.pipeline import Profile
from factory.pipeline.throttle import WindowUsage
from factory.ports.executor import (
    AgentKit,
    AttemptEvent,
    AttemptRequest,
    AttemptResult,
    AttemptStats,
    ModelTokens,
    Usage,
    attempt_event,
    card_fact,
    event_line,
)
from factory.ports.kind import Kind
from factory.ports.sandbox import Credential, Needs, Process, Sandbox, SandboxError

# Where a Target project keeps the skills that Claude Code loads for whoever works in it.
PROJECT_SKILLS = Path(".claude") / "skills"

# What Claude Code adds to the Agent kit: the plugin manifest and hooks, the settings and what the CLI loads itself.
KIT = Path(__file__).with_name("claude_code_kit")
# Bump it whenever `Kit.assemble` lays out or translates the Agent kit differently, so the variant changes too.
TRANSLATION = "1"

# The first three would send the agent's requests past the subscription, and the agent never needs GitHub.
# The agent loads only the Kit, so the plugin directories a host terminal such as Orca's adds are scrubbed too.
SCRUBBED_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "GH_TOKEN",
    "CLAUDE_CODE_PLUGIN_DIRS",
)
SETTING_SOURCES: dict[Profile, str] = {"project": "project", "hermetic": ""}
AUTH_STATUSES = (401, 403)
# The tools by which Claude Code starts a subagent, whose call is the subagent's life.
SUBAGENT_TOOLS = ("Agent", "Task")

# The length in seconds of each subscription window a `rate_limit_event` may tell of, by the name Claude gives it.
WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}

# The agent needs only the API, because the proxy of the Sandbox refreshes the OAuth on the host.
# `claude` refuses to start without some token, and the proxy replaces whatever it sends.
HOSTS = ("api.anthropic.com",)
SUBSCRIPTION = Credential(
    service="anthropic",
    env="CLAUDE_CODE_OAUTH_TOKEN",
    placeholder="sbx-proxy-managed",
    missing="no Claude subscription: run `sbx run claude`, sign in with /login once, then remove that Sandbox",
)
STDERR_LINES = 20

# The agent a Sandbox that sets agents up by name hosts for Claude Code.
AGENT = "claude"

# `claude --model` takes these aliases or a full model ID such as `claude-sonnet-4-5-20250929`, with an optional `[1m]`.
MODEL_ALIASES = ("sonnet", "opus", "haiku", "opusplan")
MODEL_ID = re.compile(r"claude-[a-z0-9][a-z0-9.-]*")
LONG_CONTEXT = "[1m]"
EFFORTS = ("low", "medium", "high", "xhigh", "max")


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
    """A Kit is the Claude Code translation of the Agent kit, which every agent gets in place of the owner's config.

    `assemble` builds a folder of the Run that holds the plugin with the kit's skills, the settings,
    the MCP config and the instructions.
    `builtin.json` pins what the Claude Code CLI loads on its own and, in `absent_tools`, the tools
    the kit's settings keep from an agent. The contract tests check both.
    """

    def __init__(self, root: Path = KIT) -> None:
        self.root = root
        self.plugin = root / "plugin"
        self.builtin = AgentSet(**{k: frozenset(v) for k, v in _json(root / "builtin.json").items() if k in LABELS})
        self.name = str(_json(self.plugin / ".claude-plugin" / "plugin.json")["name"])

    def assemble(self, kit: AgentKit, skills: Path, into: Path) -> None:
        """Builds the Kit in `into` from `kit` and the Library skills in `skills`.

        A skill of the Library replaces the kit's skill of the same name whole.
        """
        if into.exists():
            shutil.rmtree(into)
        shutil.copytree(self.plugin, plugin_of(into))
        for skill in [*(kit.skills_dir / name for name in kit.skills), *_folders(skills)]:
            target = plugin_of(into) / "skills" / skill.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(skill, target)
        shutil.copy(self.root / "settings.json", settings_of(into))
        shutil.copy(kit.instructions, instructions_of(into))
        servers = {name: _mcp_server(server) for name, server in kit.mcp_servers().items()}
        mcp_of(into).write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n")

    def required(self, profile: Profile, workspace: Path, assembled: Path) -> AgentSet:
        """Returns the set an agent must load: the Kit `assemble` built in `assembled`, and the Target project's own.

        The Target project's skills and agents count only under `profile: project`.
        """
        plugin = plugin_of(assembled)
        kit = AgentSet(
            plugins=frozenset({f"{self.name}@inline"}),
            skills=frozenset(f"{self.name}:{name}" for name in _skills(plugin)),
            agents=frozenset(f"{self.name}:{name}" for name in _agents(plugin)),
            mcp_servers=frozenset(_json(mcp_of(assembled)).get("mcpServers", {})),
        )
        if profile == "hermetic":
            return kit
        project = workspace / ".claude"
        return kit | AgentSet(skills=frozenset(_skills(project)), agents=frozenset(_agents(project)))


def plugin_of(assembled: Path) -> Path:
    return assembled / "plugin"


def settings_of(assembled: Path) -> Path:
    return assembled / "settings.json"


def mcp_of(assembled: Path) -> Path:
    return assembled / "mcp.json"


def instructions_of(assembled: Path) -> Path:
    return assembled / "instructions.md"


def _mcp_server(server: dict[str, Any]) -> dict[str, Any]:
    """Returns the entry of `--mcp-config` for an MCP server of the Agent kit, where a `url` is a server over HTTP."""
    if "url" in server:
        return {"type": "http", **server}
    return dict(server)


def _folders(root: Path) -> list[Path]:
    return sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else []


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


class Normalizer:
    """A Normalizer turns the lines of Claude Code's `stream-json` into Attempt events.

    It keeps the name of each tool call, because Claude Code names only the call's id in the tool's result,
    and the type of each subagent's call for the same reason.
    `prompt` is what the agent was started with, which `claude -p` does not write to its stream,
    so the first init event brings its card fact.
    """

    def __init__(self, prompt: str | None = None) -> None:
        self._tools: dict[str, str] = {}
        self._subagents: dict[str, str | None] = {}
        self._prompt = prompt

    def line(self, line: bytes) -> list[AttemptEvent]:
        """Returns the events of a stream line, where a line that is no event, such as a crash message, an error."""
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if isinstance(event, dict):
            return self.event(event)
        text = line.decode(errors="replace").rstrip()
        return [attempt_event("error", text=text)] if text else []

    def event(self, event: dict[str, Any]) -> list[AttemptEvent]:
        """Returns the events of one Claude event, where an `assistant` or `user` event is progress and a heartbeat,
        a `system` event about a long command, is nothing.
        """
        kind = event.get("type")
        if _is_init(event):
            note = f"model {event.get('model')} tools {len(event.get('tools', []))}"
            return [attempt_event("text", kind="note", text=note), *self._started(event)]
        if kind == "assistant":
            said = (e for block in _blocks(event) if (e := self._said(block)))
            return [attempt_event("progress"), *said, *self._said_facts(event)]
        if kind == "user":
            results = [block for block in _blocks(event) if block.get("type") == "tool_result"]
            ended = [self._ended(block) for block in results]
            return [attempt_event("progress"), *ended, *(f for block in results for f in self._ended_facts(block))]
        if kind == "rate_limit_event":
            info = event.get("rate_limit_info")
            info = info if isinstance(info, dict) else {}
            windows = {
                name: {"utilization": usage.used, "resets_at": usage.resets_at.timestamp(), "seconds": usage.seconds}
                for name, usage in window_usages(event).items()
            }
            return [
                attempt_event("windows", status=info.get("status"), resets_at=info.get("resetsAt"), windows=windows)
            ]
        if kind == "result":
            usage = _usage(event)
            result = attempt_event(
                "result",
                status=event.get("subtype"),
                turns=event.get("num_turns"),
                duration_ms=event.get("duration_ms"),
                text=event.get("result") or "",
            )
            reply = [card_fact("reply", text=event["result"])] if event.get("result") else []
            return [attempt_event("usage", **asdict(usage)), *reply, card_fact("turn_end"), result]
        return []

    def _started(self, init: dict[str, Any]) -> list[AttemptEvent]:
        """Returns the card facts of the session, and of the prompt once, because an agent may repeat its init event."""
        facts = [card_fact("session", session_id=init.get("session_id"))]
        if self._prompt is not None:
            facts.append(card_fact("prompt", text=self._prompt))
            self._prompt = None
        return facts

    def _said_facts(self, event: dict[str, Any]) -> list[AttemptEvent]:
        """Returns the card facts of a message, where only the main agent's text is a reply."""
        facts = []
        for block in _blocks(event):
            if block.get("type") == "tool_use":
                facts += self._called(block)
            elif block.get("type") == "text" and block.get("text", "").strip() and not event.get("parent_tool_use_id"):
                facts.append(card_fact("reply", text=block["text"].rstrip()))
        return facts

    def _called(self, block: dict[str, Any]) -> list[AttemptEvent]:
        call, given = str(block.get("id")), block.get("input", {})
        facts = [card_fact("tool", id=call, name=str(block.get("name")), input=given)]
        if block.get("name") in SUBAGENT_TOOLS:
            kind = given.get("subagent_type") if isinstance(given, dict) else None
            self._subagents[call] = kind
            facts.append(card_fact("subagent_start", id=call, agent_type=kind))
        return facts

    def _ended_facts(self, block: dict[str, Any]) -> list[AttemptEvent]:
        call = str(block.get("tool_use_id"))
        facts = []
        if call in self._subagents:
            facts.append(card_fact("subagent_stop", id=call, agent_type=self._subagents.pop(call)))
        facts.append(card_fact("tool_output", id=call, output=_plain(block.get("content", ""))))
        return facts

    def _said(self, block: dict[str, Any]) -> AttemptEvent | None:
        kind = block.get("type")
        if kind == "text" and block.get("text", "").strip():
            return attempt_event("text", kind="message", text=block["text"].rstrip())
        if kind == "thinking" and block.get("thinking", "").strip():
            return attempt_event("text", kind="thinking", text=block["thinking"])
        if kind == "tool_use":
            name = str(block.get("name"))
            self._tools[str(block.get("id"))] = name
            return attempt_event("tool", name=name, target=_plain(block.get("input", {})), status="started")
        return None

    def _ended(self, block: dict[str, Any]) -> AttemptEvent:
        return attempt_event(
            "tool",
            name=self._tools.get(str(block.get("tool_use_id"))),
            status="failed" if block.get("is_error") else "done",
            output=_plain(block.get("content", "")),
        )


def _plain(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def normalize(stream: Path, events: Path) -> None:
    """Writes the Attempt events of a whole raw stream to `events`, for an Attempt from before the Executor wrote them.

    It writes them whole or not at all, so a reader that finds `events` finds every event.
    """
    normalizer = Normalizer()
    lines = stream.read_bytes().split(b"\n")
    written = events.with_name(f"{events.name}.tmp")
    written.write_bytes(b"".join(event_line(e) for line in lines for e in normalizer.line(line)))
    written.replace(events)


class ClaudeCodeExecutor:
    """ClaudeCodeExecutor starts `command` in the Sandbox with the Kit's launch flags and a scrubbed environment."""

    name = "claude-code"
    default_model = "sonnet"
    reported_windows = tuple(WINDOW_SECONDS)

    def __init__(self, command: Sequence[str] = ("claude",), kit: Kit | None = None) -> None:
        self.command = tuple(command)
        self.kit = kit or Kit()
        self._windows: dict[str, WindowUsage] = {}

    def needs(self) -> Needs:
        return Needs(egress=HOSTS, credentials=(SUBSCRIPTION,), agent=(AGENT,))

    def settings_problem(self, model: str, effort: str | None) -> str | None:
        """Every model of Claude Code takes every effort, so it checks each alone."""
        base = model.removesuffix(LONG_CONTEXT)
        if base not in MODEL_ALIASES and not MODEL_ID.fullmatch(base):
            aliases = ", ".join(MODEL_ALIASES)
            return f"{model} is not a model of the {self.name} Executor; use {aliases} or a claude- model ID"
        if effort is not None and effort not in EFFORTS:
            return f"{effort} is not an effort of the {self.name} Executor; use {', '.join(EFFORTS)}"
        return None

    def reads(self) -> list[Path]:
        """Reads nothing besides the Run directory, which holds the Kit `assemble` built."""
        return []

    def translation_version(self) -> str:
        """Returns `TRANSLATION` with the hash of the files the Kit adds to the Agent kit."""
        return f"{TRANSLATION}:{tree_sha(self.kit.root)}"

    def assemble(self, kit: AgentKit, skills: Path, into: Path) -> None:
        self.kit.assemble(kit, skills, into)

    def windows(self) -> dict[str, WindowUsage]:
        return dict(self._windows)

    def start(self, request: AttemptRequest, sandbox: Sandbox) -> "ClaudeCodeHandle":
        request.agent_home.mkdir(parents=True, exist_ok=True)
        stderr = request.attempt_dir / "stderr.log"
        argv = [*self._env(request), *self.command, *self._flags(request)]
        request.events.write_bytes(b"")
        process = sandbox.exec(request.sandbox, argv, stdout=request.stream, stderr=stderr)
        required = self.kit.required(request.settings.profile, request.workspace, request.kit)
        return ClaudeCodeHandle(
            process, request.stream, stderr, required, self.kit.builtin, request.events, self._windows, _prompt(request)
        )

    def _env(self, request: AttemptRequest) -> list[str]:
        """Returns the `env` prefix that scrubs API credentials and points the agent at its own config."""
        unset = [arg for name in SCRUBBED_ENV for arg in ("-u", name)]
        return ["env", *unset, f"CLAUDE_CONFIG_DIR={request.agent_home}", "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1"]

    def _flags(self, request: AttemptRequest) -> list[str]:
        settings = request.settings
        return [
            "-p",
            _prompt(request),
            "--model",
            settings.model,
            *(["--effort", settings.effort] if settings.effort else []),
            "--output-format",
            "stream-json",
            "--verbose",
            "--json-schema",
            request.output_schema.read_text(),
            "--dangerously-skip-permissions",
            "--setting-sources",
            SETTING_SOURCES[settings.profile],
            "--plugin-dir",
            str(plugin_of(request.kit)),
            "--settings",
            str(settings_of(request.kit)),
            "--strict-mcp-config",
            "--mcp-config",
            str(mcp_of(request.kit)),
            "--append-system-prompt-file",
            str(instructions_of(request.kit)),
        ]


def _prompt(request: AttemptRequest) -> str:
    return f"{request.prompt.read_text().rstrip()}\n\nYour Brief is {request.brief}.\n"


class ClaudeCodeHandle:
    """A ClaudeCodeHandle reads the agent's stream as it grows and writes its Attempt events to `events`.

    It kills the agent as soon as the init event shows a set other than the factory's.
    """

    def __init__(
        self,
        process: Process,
        stream: Path,
        stderr: Path,
        required: AgentSet,
        builtin: AgentSet,
        events: Path,
        windows: dict[str, WindowUsage] | None = None,
        prompt: str | None = None,
    ) -> None:
        self._process = process
        self._events_file = events
        self._normalizer = Normalizer(prompt)
        self._windows = {} if windows is None else windows
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
        self._read(final=code is not None)
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
        with contextlib.suppress(OSError):
            self._read(final=True)

    def _oom(self, crash: AttemptResult) -> AttemptResult:
        """Tells a crash that the Sandbox's OOM killer caused from any other, by the Sandbox's own count."""
        memory = self._process.memory()
        if memory is None or memory.oom_kills == 0:
            return crash
        limit = f" with {memory.limit}" if memory.limit else ""
        reason = f"the OOM killer killed the agent in a Sandbox{limit}: {crash.reason}"
        return AttemptResult(
            output=None, failure="oom", reason=reason, memory=memory, usage=crash.usage, stats=crash.stats
        )

    def _read(self, final: bool = False) -> None:
        """Reads the stream's new complete lines, and once the agent has ended, the incomplete last line as well.

        Only the Attempt events take the incomplete line, which a crashed agent may leave.
        """
        with self._stream.open("rb") as stream:
            stream.seek(self._offset)
            data = stream.read()
        complete, end, rest = data.rpartition(b"\n")
        self._offset += len(complete) + len(end)
        normalized = []
        for line in complete.splitlines():
            normalized += self._normalizer.line(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                self._events.append(event)
                self._windows.update(window_usages(event))
        if final and rest:
            self._offset += len(rest)
            normalized += self._normalizer.line(rest)
        if normalized:
            with self._events_file.open("ab") as events:
                events.write(b"".join(event_line(e) for e in normalized))

    def _stderr_tail(self) -> str:
        if not self._stderr.is_file():
            return ""
        lines = self._stderr.read_text(errors="replace").strip().splitlines()
        return "\n".join(lines[-STDERR_LINES:])


def window_usages(event: dict[str, Any]) -> dict[str, WindowUsage]:
    """Returns the usage of each subscription window a `rate_limit_event` tells, which is none for any other event.

    The info lists every window in `unifiedWindows`, or tells just one by its `rateLimitType`.
    A window whose length the Executor does not know is left out.
    """
    info = event.get("rate_limit_info") if event.get("type") == "rate_limit_event" else None
    if not isinstance(info, dict):
        return {}
    listed = info.get("unifiedWindows")
    windows = listed if isinstance(listed, dict) else {info.get("rateLimitType"): info}
    found = {}
    for name, window in windows.items():
        if name not in WINDOW_SECONDS or not isinstance(window, dict):
            continue
        used, resets_at = _count_or_share(window.get("utilization")), _count_or_share(window.get("resetsAt"))
        if used is not None and resets_at is not None:
            found[name] = WindowUsage(used, datetime.fromtimestamp(resets_at, UTC), WINDOW_SECONDS[name])
    return found


def _count_or_share(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _is_init(event: dict[str, Any]) -> bool:
    return event.get("type") == "system" and event.get("subtype") == "init"


def attempt_result(events: list[dict[str, Any]], code: int, stderr: str) -> AttemptResult:
    """Returns how the Attempt ended from its exit code and stream, with what it spent when it gave a result."""
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    usage = _usage(result) if result is not None else None
    return replace(_outcome(events, result, code, stderr), usage=usage, stats=attempt_stats(events))


def _usage(result: dict[str, Any]) -> Usage:
    """Returns what the `result` event says the Attempt spent, at the cost Claude Code reports.

    The tokens come from `modelUsage`, because `usage` counts only the main agent and leaves its subagents out.
    Claude's `inputTokens` already leave out the cache reads, and the thinking tokens are known only where a model
    tells them.
    """
    models = [m for m in (result.get("modelUsage") or {}).values() if isinstance(m, dict)]

    def tokens(name: str) -> int:
        return sum(int(m.get(name) or 0) for m in models)

    thinking = [count for m in models if (count := _count(m.get("thinkingTokens"))) is not None]
    return Usage(
        cost_usd=float(result.get("total_cost_usd") or 0),
        cost_source="reported",
        turns=int(result.get("num_turns") or 0),
        input_tokens=tokens("inputTokens"),
        output_tokens=tokens("outputTokens"),
        cache_read_tokens=tokens("cacheReadInputTokens"),
        cache_write_tokens=tokens("cacheCreationInputTokens"),
        thinking_tokens=sum(thinking) if thinking else None,
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


def attempt_stats(events: list[dict[str, Any]]) -> AttemptStats:
    """Folds the agent's stream into what it tells about the Attempt, leaving unknown what the stream does not say.

    `num_turns` counts only the main agent, so the requests are the unique `message.id` of every `assistant` event,
    which includes the subagents' messages.
    """
    result = next((e for e in reversed(events) if e.get("type") == "result"), None) or {}
    assistant = [e for e in events if e.get("type") == "assistant" and isinstance(e.get("message"), dict)]
    ids = {e["message"]["id"] for e in assistant if e["message"].get("id")}
    models = {n: m for n, m in (result.get("modelUsage") or {}).items() if isinstance(m, dict)}
    subagents = result.get("subagent_stats")
    subagents = subagents if isinstance(subagents, dict) else {}
    by_type = subagents.get("by_type")
    denials = result.get("permission_denials")
    windows = [
        info
        for e in events
        if e.get("type") == "rate_limit_event" and isinstance(info := e.get("rate_limit_info"), dict)
    ]
    return AttemptStats(
        tokens={name: _model_tokens(usage) for name, usage in models.items()} if models else None,
        api_requests=len(ids) if ids else None,
        turns=_count(result.get("num_turns")),
        duration_ms=_count(result.get("duration_ms")),
        duration_api_ms=_count(result.get("duration_api_ms")),
        subagents_started=_count(subagents.get("started")),
        subagents_failed=_count(subagents.get("failed")),
        subagents_by_type={str(k): int(v) for k, v in by_type.items()} if isinstance(by_type, dict) else None,
        tool_calls=_tool_calls(assistant) if assistant else None,
        tool_errors=_tool_errors(events) if assistant else None,
        permission_denials=len(denials) if isinstance(denials, list) else None,
        peak_context_tokens=_peak_context(assistant),
        context_window=max(
            (c for m in models.values() if (c := _count(m.get("contextWindow"))) is not None), default=None
        ),
        utilization_start=_utilization(windows, first=True),
        utilization_end=_utilization(windows, first=False),
    )


def _count(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _model_tokens(usage: dict[str, Any]) -> ModelTokens:
    return ModelTokens(
        input_tokens=_count(usage.get("inputTokens")),
        output_tokens=_count(usage.get("outputTokens")),
        thinking_tokens=_count(usage.get("thinkingTokens")),
        cache_read_tokens=_count(usage.get("cacheReadInputTokens")),
        cache_write_tokens=_count(usage.get("cacheCreationInputTokens")),
    )


def _tool_calls(assistant: list[dict[str, Any]]) -> dict[str, int]:
    calls: dict[str, int] = {}
    for event in assistant:
        for block in _blocks(event):
            if block.get("type") == "tool_use":
                name = str(block.get("name"))
                calls[name] = calls.get(name, 0) + 1
    return calls


def _tool_errors(events: list[dict[str, Any]]) -> int:
    return sum(
        1
        for e in events
        if e.get("type") == "user"
        for block in _blocks(e)
        if block.get("type") == "tool_result" and block.get("is_error")
    )


def _blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    content = (event.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _peak_context(assistant: list[dict[str, Any]]) -> int | None:
    """Returns the largest context the main agent sent in one request, which its subagents' own contexts leave out."""
    sizes = []
    for event in assistant:
        usage = event["message"].get("usage")
        if event.get("parent_tool_use_id") or not isinstance(usage, dict):
            continue
        parts = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        sizes.append(sum(_count(usage.get(part)) or 0 for part in parts))
    return max(sizes, default=None)


def _utilization(windows: list[dict[str, Any]], first: bool) -> dict[str, float] | None:
    """Returns the share used of each subscription window by its first or last `rate_limit_event`."""
    found: dict[str, float] = {}
    for info in windows if first else reversed(windows):
        name, used = info.get("rateLimitType"), info.get("utilization")
        if isinstance(name, str) and isinstance(used, int | float) and not isinstance(used, bool):
            found.setdefault(name, float(used))
    return found or None


KIND = Kind[ClaudeCodeExecutor](lambda settings, tools: ClaudeCodeExecutor())
