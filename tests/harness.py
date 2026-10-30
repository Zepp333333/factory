"""The Seam 1 harness drives the whole `factory` CLI on a temporary Target project.

Only two ports are replaced.
`ScriptedExecutor` runs the real `ClaudeCodeExecutor` on an agent that plays a per-Stage script,
and `LocalSandbox` runs it straight in the Workspace.
SQLite, host git, `TerminalWorkbench`, `LocalTracker`, `GitHubTracker` and Config stay real, and the clock is injected.
`GitHubTracker` talks to the fake `gh` and `GitLabTracker` to the fake `glab`,
and the Target project's `origin` points at github.com but pushes to a bare repository next to it.
"""

import atexit
import contextlib
import functools
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml

from factory import spawn
from factory.claude_code import ClaudeCodeExecutor
from factory.cli import main
from factory.config import Project, load_project
from factory.executor import AttemptHandle, AttemptRequest, AttemptResult
from factory.library_lookup import Layer, Library
from factory.orchestrator import TICK_SECONDS, Orchestrator
from factory.recovery import replay
from factory.run_state import TicketRuntime
from factory.sandbox import LocalSandbox, Process, Sandbox, SandboxError, SandboxSettings
from factory.sbx import SbxSandbox
from factory.throttle import WindowUsage
from factory.wiring import Machine
from factory.workbench import TerminalWorkbench, Workbench
from fake_gh import INITIAL
from fake_glab import INITIAL as GLAB_INITIAL
from scripted_agent import parked_marker

AGENT = Path(__file__).with_name("scripted_agent.py")
FAKE_SBX = Path(__file__).with_name("fake_sbx.py")
FAKE_GH = Path(__file__).with_name("fake_gh.py")
FAKE_GLAB = Path(__file__).with_name("fake_glab.py")
FAKE_ORCA = Path(__file__).with_name("fake_orca.py")
REPO = "owner/target"
FACTORY_LIBRARY = Path(__file__).resolve().parents[1] / "src" / "factory" / "library"

# The limit resets 5 minutes after FakeClock's start, short of `RATE_LIMIT_PAUSE`, so a test tells the two waits apart.
# Every heartbeat of the pause reconciles the Ticket with git, so a longer pause makes the test slower.
RESETS_AT = datetime(2026, 10, 20, 9, 5, 0, tzinfo=UTC)

IMPLEMENTER_SCHEMA = {
    "type": "object",
    "required": ["handoff"],
    "properties": {"summary": {"type": "string"}, "handoff": {"type": "string"}},
    "additionalProperties": False,
}

PIPELINE = {
    "start": "implement",
    "escalate_to": "abandoned",
    "defaults": {"model": "sonnet"},
    "roles": {
        "implementer": {"prompt": "prompts/implementer.md", "output": "schemas/implementer.json"},
        "describer": {"prompt": "prompts/describer.md", "output": "schemas/describer.json", "skills": ["pr"]},
    },
    "stages": {"implement": {"role": "implementer", "inputs": ["spec", "ticket"], "next": {"done": "sync"}}},
    "operations": {
        "sync": {"kind": "sync", "next": {"clean": "merge", "conflict": "abandoned"}},
        "merge": {"kind": "merge", "next": {"merged": "done"}},
    },
}


# The project file names the Pipeline of the Harness's config, which sits next to the Target project.
# The Run's sleep block is off unless a test asks for it, so no test starts a helper of the host.
PROJECT = {"pipeline": {"file": "../config/pipeline.yaml"}, "base": "main", "prevent_sleep": False}


def load_real_project(path: Path) -> Project:
    """Loads a project file of this repository with a Machine whose home is a fresh directory."""
    machine = Machine(home=Path(tempfile.mkdtemp()))
    return load_project(path, machine.library, machine.kinds)


class FakeClock:
    """FakeClock starts at a fixed instant and moves forward only when the Run sleeps.

    `on_sleep` runs before each sleep, so a test can look at a Run while it waits.
    A sleep takes no real time while `parked` says that only scripted agents that wait to be stopped are running,
    or when it is longer than a tick, which only the Run's pause is, and the Run pauses only when nothing runs.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)
        self.on_sleep: Callable[[float], None] | None = None
        self.parked: Callable[[], bool] = lambda: False

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        if self.on_sleep is not None:
            self.on_sleep(seconds)
        self._now += timedelta(seconds=seconds)

        # The scripted agent is a real process, so the Run still yields the CPU to it while it works.
        if seconds <= TICK_SECONDS and not self.parked():
            time.sleep(min(seconds, 0.005))


class FrozenClock(FakeClock):
    """FrozenClock stays at its start instant, so no heartbeat the Run beats after it looks stale to it."""

    def sleep(self, seconds: float) -> None:
        time.sleep(min(seconds, 0.005))


@dataclass
class AgentScript:
    """An AgentScript lists the files the scripted agent writes in one Attempt and the JSON it returns.

    The agent first deletes the paths in `remove`, then writes `files` and runs the shell commands in `run`.
    `fail` makes the agent `crash`, `hang`, fail `auth`, get `rate_limited` or run out of turns with `max_turns`
    after it writes its files.
    A `rate_limited` agent says the limit resets at `resets_at`, or gives no reset time when it is None.
    With `wait_for` set, the agent waits after its init event until that file exists, while the Run polls it.
    With `heartbeat` set, the agent prints a heartbeat of a long command after its init event and nothing else.
    The result carries `turns`, `cost` and, when set, `tokens` as input, output, cache read and cache write,
    which the agent splits over a model and a subagent in `modelUsage`.
    `leak` adds plugins, skills, agents or MCP servers outside the factory's set to the agent's init event.
    """

    output: dict[str, Any] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    remove: list[str] = field(default_factory=list)
    run: list[str] = field(default_factory=list)
    fail: Literal["crash", "hang", "auth", "rate_limited", "max_turns"] | None = None
    resets_at: datetime | None = RESETS_AT
    leak: dict[str, list[str]] = field(default_factory=dict)
    wait_for: Path | None = None
    heartbeat: bool = False
    turns: int = 1
    cost: float = 0
    tokens: tuple[int, int, int, int] | None = None


def ticket_text(title: str, blocked_by: list[str] | None = None) -> str:
    """Returns a LocalTracker Ticket file that asks for `<title>.txt`."""
    meta = yaml.safe_dump({"title": title, **({"blocked_by": blocked_by} if blocked_by else {})})
    return f"---\n{meta}---\nWrite {title}.txt.\n"


def writes(name: str, command: str | None = None, wait_for: Path | None = None) -> AgentScript:
    """Returns an agent that writes `<name>.txt`, or runs `command` in its place."""
    files = {} if command else {f"{name}.txt": f"{name}\n"}
    run = [command] if command else []
    return AgentScript(files=files, run=run, output={"summary": f"Add {name}", "handoff": "done"}, wait_for=wait_for)


class ScriptedHandle:
    """ScriptedHandle polls the Attempt only once the scripted agent has written its files.

    The agent prints its init event only after its files are on disk, so the fake clock never
    times out an Attempt that has not started yet.
    """

    def __init__(self, handle: AttemptHandle, stream: Path) -> None:
        self._handle = handle
        self._stream = stream
        self._started = False

    def poll(self) -> AttemptResult | None:
        deadline = time.monotonic() + 10
        while not self._started and time.monotonic() < deadline:
            self._started = self._stream.stat().st_size > 0
            if not self._started:
                result = self._handle.poll()
                if result is not None:
                    return result
                time.sleep(0.005)
        return self._handle.poll()

    def kill(self) -> None:
        self._handle.kill()


class ScriptedExecutor:
    """ScriptedExecutor runs the real ClaudeCodeExecutor with the scripted agent in place of `claude`.

    It plays each Stage from its script, or from a list taken one per Attempt of a Ticket.
    It reports the `window_usage` a test sets, and `on_start` lets the usage change as an Attempt starts.
    A script in `ticket_scripts` under a Ticket and a Stage plays instead for that Ticket.
    """

    def __init__(self) -> None:
        self.scripts: dict[str, AgentScript | list[AgentScript]] = {}
        self.ticket_scripts: dict[tuple[str, str], AgentScript | list[AgentScript]] = {}
        self.requests: list[AttemptRequest] = []
        self.window_usage: dict[str, WindowUsage] = {}
        self.on_start: Callable[[AttemptRequest], None] | None = None

    def windows(self) -> dict[str, WindowUsage]:
        """Returns the usage the test injected, as an Executor that reports its windows would."""
        return dict(self.window_usage)

    def reads(self) -> list[Path]:
        return ClaudeCodeExecutor().reads()

    def kit_sha(self, skills: Path) -> str:
        return ClaudeCodeExecutor().kit_sha(skills)

    def assemble(self, skills: Path, into: Path) -> None:
        ClaudeCodeExecutor().assemble(skills, into)

    def start(self, request: AttemptRequest, sandbox: Sandbox) -> ScriptedHandle:
        played = sum(1 for e in self.requests if (e.sandbox, e.stage) == (request.sandbox, request.stage))
        self.requests.append(request)
        if self.on_start is not None:
            self.on_start(request)
        ticket = json.loads(request.brief.read_text()).get("ticket")
        scripts = self.ticket_scripts.get((ticket, request.stage)) or self.scripts[request.stage]
        script = scripts[min(played, len(scripts) - 1)] if isinstance(scripts, list) else scripts
        scenario = request.attempt_dir / "scenario.json"
        scenario.write_text(
            json.dumps(
                {
                    "remove": script.remove,
                    "files": script.files,
                    "run": script.run,
                    "output": script.output,
                    "fail": script.fail,
                    "resets_at": int(script.resets_at.timestamp()) if script.resets_at else None,
                    "leak": script.leak,
                    "wait_for": str(script.wait_for) if script.wait_for else None,
                    "heartbeat": script.heartbeat,
                    "turns": script.turns,
                    "cost": script.cost,
                    "tokens": script.tokens,
                }
            )
        )
        executor = ClaudeCodeExecutor(command=(sys.executable, str(AGENT), str(scenario)))
        return ScriptedHandle(executor.start(request, sandbox), request.stream)

    def launch(self, request: AttemptRequest) -> dict[str, Any]:
        """Returns the arguments, environment and stdin the scripted agent of `request` was started with."""
        launch: dict[str, Any] = json.loads((request.attempt_dir / "launch.json").read_text())
        return launch


def scenario_of(argv: list[str]) -> Path | None:
    """Returns the scenario a scripted agent started with `argv` plays, or None for any other process."""
    return Path(argv[argv.index(str(AGENT)) + 1]) if str(AGENT) in argv else None


class FlakySandbox(LocalSandbox):
    """FlakySandbox is a LocalSandbox that refuses to start the next `refuse` agents and `lost_probes` Probes.

    With `refuse_node` set it refuses only the agents of that node.
    It keeps every process it started in `started`, so a test can see whether one was stopped,
    and `parked` tells the clock whether the only ones still running are scripted agents that wait to be stopped.
    With `refuse_remove` set it also refuses to remove a Sandbox, and with `left_behind` set it reports that reason
    for a Sandbox that stays behind.
    """

    def __init__(self) -> None:
        super().__init__()
        self.refuse = 0
        self.refuse_node: str | None = None
        self.lost_probes = 0
        self.refuse_remove = False
        self.left_behind: str | None = None
        self.started: list[Process] = []
        self._parked: dict[Process, Path] = {}

    def parked(self) -> bool:
        """Returns whether a scripted agent that waits to be stopped runs, and every other process has ended."""
        running = [process for process in self.started if process.poll() is None]
        markers = [self._parked.get(process) for process in running]
        return bool(running) and all(marker is not None and marker.exists() for marker in markers)

    def exec(
        self, name: str, argv: list[str], stdout: Path, env: dict[str, str] | None = None, stderr: Path | None = None
    ) -> Process:
        probe = env is not None and "GATE_OUT" in env
        if probe and self.lost_probes:
            self.lost_probes -= 1
            raise SandboxError(f"Sandbox {name} did not start")
        if not probe and self.refuse and self.refuse_node in (None, *stdout.parts):
            self.refuse -= 1
            raise SandboxError(f"Sandbox {name} did not start")
        process = super().exec(name, argv, stdout, env, stderr)
        self.started.append(process)
        scenario = scenario_of(argv)
        if scenario is not None:
            self._parked[process] = parked_marker(scenario)
        return process

    def remove(self, name: str) -> str | None:
        if self.refuse_remove:
            raise SandboxError(f"Sandbox {name} did not stop")
        if self.left_behind is not None:
            super().remove(name)
            return self.left_behind
        return super().remove(name)


class LingeringWorkbench(TerminalWorkbench):
    """LingeringWorkbench is a TerminalWorkbench with a process left behind in the Sandbox.

    The process writes `late_files` into the Workspace right before the Workbench removes it,
    and with `late_commit` set it also commits on the Ticket branch, so the branch moves on after the engine saved it.
    """

    def __init__(self) -> None:
        self.late_files: dict[str, str] = {}
        self.late_commit = False

    def remove_workspace(self, repo: Path, path: Path) -> None:
        for name, text in self.late_files.items():
            (path / name).write_text(text)
        if self.late_commit:
            git(path, "commit", "-q", "--allow-empty", "-m", "late")
        super().remove_workspace(repo, path)


class FakeSbx:
    """FakeSbx holds the state of the fake `sbx` CLI, which runs every Sandbox command on the host."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        self.command = (sys.executable, str(FAKE_SBX), str(root))

    def calls(self, command: str) -> list[list[str]]:
        """Returns the arguments of every call of `command`, such as `create` or `exec`."""
        path = self.root / "calls.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [call[1:] for call in map(json.loads, lines) if call[0] == command]

    def state(self) -> dict[str, Any]:
        path = self.root / "state.json"
        return json.loads(path.read_text()) if path.exists() else {"sandboxes": {}}

    def set(self, **values: Any) -> None:
        """Sets top-level state such as `oauth`, `allow_all` or `rm_fails`."""
        subprocess.run([*self.command, "ls", "--json"], check=True, capture_output=True)
        path = self.root / "state.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **values}))


class FakeOrca:
    """FakeOrca holds the state of the fake `orca` CLI, which keeps worktree comments and runs terminals on the host."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        self.command = (sys.executable, str(FAKE_ORCA), str(root))

    def calls(self, *command: str) -> list[list[str]]:
        """Returns the arguments of every call that starts with `command`, such as `worktree`, `set`."""
        path = self.root / "calls.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [call for call in map(json.loads, lines) if call[: len(command)] == list(command)]

    def state(self) -> dict[str, Any]:
        path = self.root / "state.json"
        return json.loads(path.read_text()) if path.exists() else {"repos": [], "comments": {}, "terminals": {}}

    def output(self, handle: str) -> str:
        """Returns what the command of a terminal printed."""
        return (self.root / f"{handle}.out").read_text()


class FakeGh:
    """FakeGh holds the state of the fake `gh` CLI, which serves issues and pull requests from a JSON file."""

    def __init__(self, root: Path, remote: Path) -> None:
        self.root = root
        root.mkdir()
        self.command = (sys.executable, str(FAKE_GH), str(root))
        self.write({**INITIAL, "remote": str(remote)})

    def issue(
        self,
        number: int,
        title: str,
        body: str = "",
        state: str = "open",
        sub_issues: list[int | str] | None = None,
        blocked_by: list[int | str] | None = None,
        repo: str = REPO,
    ) -> str:
        """Publishes an issue and returns its URL; a link may name an issue of another repository as `owner/repo#12`."""
        data = self.state()
        issues = data["repos"].setdefault(repo, {"issues": {}})["issues"]
        issues[str(number)] = {
            "title": title,
            "body": body,
            "state": state,
            "sub_issues": [str(ref) for ref in sub_issues or []],
            "blocked_by": [str(ref) for ref in blocked_by or []],
        }
        self.write(data)
        return f"https://github.com/{repo}/issues/{number}"

    def edit(self, number: int, **fields: Any) -> None:
        data = self.state()
        data["repos"][REPO]["issues"][str(number)].update(fields)
        self.write(data)

    def set(self, **values: Any) -> None:
        """Sets top-level state such as `denied` or `pr_fails`."""
        self.write({**self.state(), **values})

    def calls(self, command: str) -> list[list[str]]:
        path = self.root / "calls.jsonl"
        lines = path.read_text().splitlines() if path.exists() else []
        return [call[1:] for call in map(json.loads, lines) if call[0] == command]

    def checks(self, *snapshots: list[dict[str, str]], sha: str = "*") -> None:
        """Sets the checks of a commit, or of the commit the PR's branch is at, as a list of snapshots over time."""
        data = self.state()
        data["checks"][sha] = list(snapshots)
        self.write(data)

    def log(self, job: int, text: str) -> None:
        """Sets what `gh run view --log-failed` prints for a job."""
        data = self.state()
        data["logs"][str(job)] = text
        self.write(data)

    def issue_state(self, number: int) -> dict[str, Any]:
        """Returns the issue as the fake holds it, with its `labels` and `comments`."""
        issue: dict[str, Any] = self.state()["repos"][REPO]["issues"][str(number)]
        return issue

    def pulls(self) -> list[dict[str, Any]]:
        pulls: list[dict[str, Any]] = self.state()["pulls"]
        return pulls

    def state(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.root / "state.json").read_text())
        return data

    def write(self, data: dict[str, Any]) -> None:
        (self.root / "state.json").write_text(json.dumps(data))


class FakeGlab:
    """FakeGlab holds the state of the fake `glab` CLI, which serves work items from a JSON file."""

    def __init__(self, root: Path, remote: Path) -> None:
        self.root = root
        root.mkdir()
        self.command: tuple[str, ...] = (sys.executable, str(FAKE_GLAB), str(root))
        self.write({**json.loads(json.dumps(GLAB_INITIAL)), "remote": str(remote)})

    def issue(
        self,
        iid: int,
        title: str,
        description: str = "",
        state: str = "opened",
        kind: str = "Task",
        children: list[int] | None = None,
        blocked_by: list[int | str] | None = None,
        project: str = "group/sub/project",
        host: str = "gitlab.example.com",
    ) -> str:
        """Publishes a work item and returns its URL; `blocked_by` holds its native links."""
        data = self.state()
        items = data["hosts"].setdefault(host, {"projects": {}})["projects"].setdefault(project, {"items": {}})["items"]
        items[str(iid)] = {
            "title": title,
            "description": description,
            "state": "open" if state == "opened" else state,
            "type": kind,
            "children": [str(ref) for ref in children or []],
            "blocked_by": [str(ref) for ref in blocked_by or []],
        }
        self.write(data)
        return f"https://{host}/{project}/-/issues/{iid}"

    def edit(
        self, iid: int, project: str = "group/sub/project", host: str = "gitlab.example.com", **fields: Any
    ) -> None:
        data = self.state()
        data["hosts"][host]["projects"][project]["items"][str(iid)].update(fields)
        self.write(data)

    def issue_state(
        self, iid: int, project: str = "group/sub/project", host: str = "gitlab.example.com"
    ) -> dict[str, Any]:
        """Returns the work item as the fake holds it, with its `labels` and `notes`."""
        item: dict[str, Any] = self.state()["hosts"][host]["projects"][project]["items"][str(iid)]
        return item

    def set(self, **values: Any) -> None:
        """Sets top-level state such as `denied` or `linked_items`."""
        self.write({**self.state(), **values})

    def calls(self) -> list[list[str]]:
        path = self.root / "calls.jsonl"
        return list(map(json.loads, path.read_text().splitlines())) if path.exists() else []

    def pipelines(self, *snapshots: dict[str, Any], sha: str = "*") -> None:
        """Sets the head pipeline of a commit, or of the commit the MR's branch is at, as snapshots over time."""
        data = self.state()
        data["pipelines"][sha] = list(snapshots)
        self.write(data)

    def merge_requests(self) -> list[dict[str, Any]]:
        merge_requests: list[dict[str, Any]] = self.state()["merge_requests"]
        return merge_requests

    def state(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.root / "state.json").read_text())
        return data

    def write(self, data: dict[str, Any]) -> None:
        (self.root / "state.json").write_text(json.dumps(data))


class FakeTty:
    """FakeTty is a stdin that claims to be a TTY and reads what a test types, through a pipe.

    The Run view asks `isatty` once per tick, so `type_later` can type between two ticks of the Run.
    """

    def __init__(self) -> None:
        read, self._write = os.pipe()
        self._read = os.fdopen(read, "r")
        self._later: list[tuple[int, str]] = []

    def type(self, text: str) -> None:
        os.write(self._write, text.encode())

    def type_later(self, text: str, ticks: int) -> None:
        """Types `text` when the Run view has asked `ticks` more times whether this is a TTY."""
        self._later.append((ticks, text))

    def isatty(self) -> bool:
        due = [text for ticks, text in self._later if ticks <= 1]
        self._later = [(ticks - 1, text) for ticks, text in self._later if ticks > 1]
        for text in due:
            self.type(text)
        return True

    def fileno(self) -> int:
        return self._read.fileno()

    def close(self) -> None:
        self._read.close()
        os.close(self._write)


@dataclass
class CliResult:
    code: int
    out: str
    err: str = ""


def git(repo: Path, *args: str) -> str:
    proc = spawn.run(["git", "-C", str(repo), *args])
    proc.check_returncode()
    return proc.stdout.strip()


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.repo = root / "target"
        self.tracker = root / "tracker"
        self.config = root / "config"
        self.clock = FakeClock()
        self.executor = ScriptedExecutor()
        self.sandbox = FlakySandbox()
        self.sbx_used = False
        self.clock.parked = lambda: not self.sbx_used and self.sandbox.parked()
        self.sbx = FakeSbx(root / "sbx")
        self.remote = root / "remote.git"
        self.gh = FakeGh(root / "gh", self.remote)
        self.glab = FakeGlab(root / "glab", self.remote)
        self.orca = FakeOrca(root / "orca")
        self.workbench = LingeringWorkbench()
        self.workbench_override: Workbench | None = None
        self.pipeline: dict[str, Any] = json.loads(json.dumps(PIPELINE))
        self.pipeline_file = self.config / "pipeline.yaml"
        self.project_file = self.repo / "factory.yaml"
        self.project: dict[str, Any] = dict(PROJECT)
        self.machine_config: dict[str, Any] = {}
        self.factory_library = root / "factory-library"
        self.keep_roles = False
        self.stdin: Any = None
        self.platform = sys.platform
        self._init_config()
        self._init_repo()
        self._write_config()

    def _init_repo(self) -> None:
        """Copies the Target project and its remote from the template and points `origin` pushes at the remote."""
        template = repo_template()
        shutil.copytree(template / "target", self.repo, symlinks=True)
        shutil.copytree(template / "remote.git", self.remote, symlinks=True)
        git(self.repo, "config", f"url.{self.remote}.insteadOf", f"git@github.com:{REPO}.git")
        for gitlab in ("git@gitlab.example.com:group/sub/project.git", "https://gitlab.com/group/sub/project.git"):
            git(self.repo, "config", "--add", f"url.{self.remote}.insteadOf", gitlab)

    def _init_config(self) -> None:
        (self.config / "prompts").mkdir(parents=True)
        (self.config / "schemas").mkdir()
        (self.config / "prompts" / "implementer.md").write_text("Implement the Ticket.\n")
        (self.config / "schemas" / "implementer.json").write_text(json.dumps(IMPLEMENTER_SCHEMA))
        library = Library([Layer("factory", FACTORY_LIBRARY)])
        for role in ("describer", "reviewer"):
            (self.config / "prompts" / f"{role}.md").write_text(library.prompt(role).text)
            (self.config / "schemas" / f"{role}.json").write_text(json.dumps(library.schema(role).document, indent=2))

    def write_spec(self, title: str, body: str, tickets: dict[str, str]) -> Path:
        spec = self.tracker / "spec"
        (spec / "tickets").mkdir(parents=True)
        (spec / "spec.md").write_text(f"# {title}\n\n{body}\n")
        for ticket_id, text in tickets.items():
            (spec / "tickets" / f"{ticket_id}.md").write_text(text)
        return spec

    def _write_config(self) -> None:
        """Writes the project file, the Pipeline file and the Roles of `pipeline["roles"]` to the factory Layer."""
        pipeline = dict(self.pipeline)
        library = self.factory_library
        roles = pipeline.get("roles", {}) if self.keep_roles else pipeline.pop("roles", {})
        for name, role in roles.items():
            files = {}
            for key, kind in (("prompt", "prompts"), ("output", "schemas")):
                source = self.config / role[key]
                if source.is_file():
                    (library / kind).mkdir(parents=True, exist_ok=True)
                    (library / kind / f"{name}{source.suffix}").write_text(source.read_text())
                    files[key] = name
            (library / "roles").mkdir(parents=True, exist_ok=True)
            data = {
                "prompt": files.get("prompt", name),
                "schema": files.get("output", name),
                "skills": role.get("skills", []),
            }
            (library / "roles" / f"{name}.yaml").write_text(yaml.safe_dump(data))
        self.pipeline_file.write_text(yaml.safe_dump(pipeline))
        self.project_file.write_text(yaml.safe_dump(self.project))

    def machine(self, clock: FakeClock | None = None) -> Machine:
        """Returns the Machine the CLI runs with, after writing the factory config of `machine_config`."""
        self._write_config()
        if self.machine_config:
            self.home.mkdir(exist_ok=True)
            (self.home / "config.yaml").write_text(yaml.safe_dump(self.machine_config))
        return Machine(
            home=self.home,
            clock=clock or self.clock,
            executor=self.executor,
            sandbox_for=self._sandbox,
            workbench=self.workbench_override or self.workbench,
            gh=self.gh.command,
            glab=self.glab.command,
            audit=self._audit,
            platform=self.platform,
            factory_library=self.factory_library,
            **({"stdin": self.stdin} if self.stdin else {}),
        )

    def _audit(self, orchestrator: Orchestrator) -> None:
        """Replays the journal of a Run that ended and asserts that the result equals the Run's live state."""
        tickets = [TicketRuntime(id=t.id, blocked_by=t.blocked_by) for t in orchestrator.snapshot.tickets]
        events = orchestrator.journal.events(orchestrator.run_id)
        replayed = replay(
            orchestrator.pipeline, tickets, events, orchestrator.project.max_parallel, orchestrator.project.throttle
        )
        assert replayed.comparable() == orchestrator.memory.comparable()

    def spawn(self, *argv: str) -> "subprocess.Popen[str]":
        """Starts `factory` with `argv` as a process of its own on this home, as another terminal would.

        The process holds its own FrozenClock at the instant of this one, so the Run's heartbeat is never stale to it.
        """
        code = (
            "import sys; from datetime import datetime; from factory.cli import main;"
            " from factory.wiring import Machine;"
            " from harness import FrozenClock;"
            " clock = FrozenClock(datetime.fromisoformat(sys.argv[1]));"
            " sys.exit(main(sys.argv[2:], Machine(clock=clock)))"
        )
        return subprocess.Popen(
            [sys.executable, "-c", code, self.clock.now().isoformat(), *argv],
            env={
                **os.environ,
                "FACTORY_HOME": str(self.home),
                "PYTHONPATH": os.pathsep.join([str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")]),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def cli(self, *argv: str) -> CliResult:
        machine = self.machine()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv), machine)
        return CliResult(code, out.getvalue(), err.getvalue())

    def _sandbox(self, settings: SandboxSettings) -> Sandbox:
        """Gives a project with `sandbox.kind: sbx` an SbxSandbox on the fake `sbx`, and any other the FlakySandbox.

        The clock cannot see the processes of an SbxSandbox, so it never takes them for parked agents.
        """
        if settings.kind != "sbx":
            return self.sandbox
        self.sbx_used = True
        return SbxSandbox(settings, command=self.sbx.command)

    def run(self, spec: Path | str, *flags: str) -> CliResult:
        return self.cli("run", str(spec), "--project", str(self.project_file), *flags)

    def remote_branches(self) -> dict[str, str]:
        """Returns the branches the Run pushed to the remote, which does not count the base the remote started with."""
        lines = git(self.remote, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").splitlines()
        return {name: commit for name, commit in (line.split(" ", 1) for line in lines) if name != "main"}

    def reject_pushes(self) -> None:
        """Makes the remote refuse every push, while it still serves fetches."""
        hook = self.remote / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("#!/bin/sh\necho rejected >&2\nexit 1\n")
        hook.chmod(0o755)

    def check(self) -> CliResult:
        return self.cli("check", "--project", str(self.project_file))

    def git(self, *args: str) -> str:
        return git(self.repo, *args)

    def branches(self) -> dict[str, str]:
        lines = self.git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").splitlines()
        return dict(line.split(" ", 1) for line in lines)

    def run_dir(self, run_id: str) -> Path:
        return self.home / "runs" / run_id

    def workspace(self, run_id: str, ticket: str) -> Path:
        return self.home / "workspaces" / run_id / ticket

    def events(self, run_id: str, type: str) -> list[dict[str, Any]]:
        with contextlib.closing(sqlite3.connect(self.home / "factory.db")) as db:
            rows = db.execute("SELECT payload FROM events WHERE run_id = ? AND type = ? ORDER BY seq", (run_id, type))
            return [json.loads(payload) for (payload,) in rows]

    def run_ids(self) -> list[str]:
        with contextlib.closing(sqlite3.connect(self.home / "factory.db")) as db:
            return [run_id for (run_id,) in db.execute("SELECT id FROM runs ORDER BY started_at")]

    def times(self, run_id: str, type: str) -> list[datetime]:
        """Returns when each event of `type` was journaled, by the injected clock."""
        with contextlib.closing(sqlite3.connect(self.home / "factory.db")) as db:
            rows = db.execute("SELECT at FROM events WHERE run_id = ? AND type = ? ORDER BY seq", (run_id, type))
            return [datetime.fromisoformat(at) for (at,) in rows]

    def run_took(self, run_id: str) -> str:
        """Returns how long the finished Run went by the injected clock, as the Run's output prints it.

        The clock moves on every idle tick while a scripted agent runs, so a slow machine makes a Run take longer.
        """
        [started] = self.times(run_id, "RunStarted")
        [finished] = self.times(run_id, "RunFinished")
        return clock_text((finished - started).total_seconds())

    def ticket_took(self, run_id: str, ticket: str) -> str:
        """Returns how long the finished Ticket ran from its claim by the injected clock, as the output prints it."""
        with contextlib.closing(sqlite3.connect(self.home / "factory.db")) as db:
            [(claimed, finished)] = db.execute(
                "SELECT claimed_at, finished_at FROM tickets WHERE run_id = ? AND id = ?", (run_id, ticket)
            ).fetchall()
        return clock_text((datetime.fromisoformat(finished) - datetime.fromisoformat(claimed)).total_seconds())

    def all_runs_events(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Returns the run id, type and payload of every event of every Run but its heartbeats, in journal order."""
        with contextlib.closing(sqlite3.connect(self.home / "factory.db")) as db:
            rows = db.execute("SELECT run_id, type, payload FROM events WHERE type != 'Heartbeat' ORDER BY seq")
            return [(run_id, type, json.loads(payload)) for (run_id, type, payload) in rows]

    def commands(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Returns the state, result and payload of every Command in the inbox, oldest first."""
        with contextlib.closing(sqlite3.connect(self.home / "factory.db")) as db:
            rows = db.execute("SELECT state, result, payload FROM commands ORDER BY id")
            return [(state, result, json.loads(payload)) for (state, result, payload) in rows]

    def waiting(self) -> list[str]:
        """Returns the Tickets whose Decision waits for a human, the longest waiting first."""
        with contextlib.closing(sqlite3.connect(self.home / "factory.db")) as db:
            return [ticket for (ticket,) in db.execute("SELECT ticket_id FROM waiting ORDER BY since, rowid")]

    def slots(self) -> list[tuple[str, str]]:
        """Returns the run id and holder of every slot taken on the machine."""
        with contextlib.closing(sqlite3.connect(self.home / "factory.db")) as db:
            return list(db.execute("SELECT run_id, ticket_id FROM slots ORDER BY run_id, ticket_id"))

    def all_events(self, run_id: str) -> list[tuple[str, dict[str, Any]]]:
        with contextlib.closing(sqlite3.connect(self.home / "factory.db")) as db:
            rows = db.execute(
                "SELECT type, payload FROM events WHERE run_id = ? AND type != 'Heartbeat' ORDER BY seq", (run_id,)
            )
            return [(type, json.loads(payload)) for (type, payload) in rows]


@functools.cache
def repo_template() -> Path:
    """Builds the Target project and its bare remote once per test process, for every Harness to copy.

    It commits the default project file, which names the Pipeline next to the Target project,
    and it has no sample hooks, which every copy would pay for.
    """
    root = Path(tempfile.mkdtemp(prefix="factory-template-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    repo = root / "target"
    repo.mkdir()
    git(repo, "init", "-q", "--template=", "-b", "main")
    git(repo, "config", "user.name", "Owner")
    git(repo, "config", "user.email", "owner@example.com")
    (repo / "README.md").write_text("# Target\n")
    (repo / "factory.yaml").write_text(yaml.safe_dump(PROJECT))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")
    git(repo, "branch", "feature")
    git(root, "init", "-q", "--template=", "--bare", "remote.git")
    git(repo, "push", "-q", str(root / "remote.git"), "main")
    git(repo, "remote", "add", "origin", f"git@github.com:{REPO}.git")
    git(repo, "remote", "add", "gitlab", "git@gitlab.example.com:group/sub/project.git")
    return root


def clock_text(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}m {whole % 60:02d}s" if whole >= 60 else f"{whole}s"


def run_id_of(out: str) -> str:
    match = re.search(r"^Run (\S+) started", out, re.MULTILINE)
    assert match, out
    return match.group(1)
