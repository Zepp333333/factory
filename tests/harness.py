"""The Seam 1 harness drives the whole `factory` CLI on a temporary Target project.

The harness registers its fakes as the kinds of the Machine's registries, under the names of the real kinds.
`ScriptedExecutor` runs the real `ClaudeCodeExecutor` on an agent that plays a per-Stage script,
`FlakySandbox` runs it straight in the Workspace, and a `LingeringWorkbench` opens its terminals.
SQLite, host git, `TerminalWorkbench`, `LocalTracker`, `GitHubTracker` and Config stay real, and the clock is injected.
`GitHubTracker` talks to the fake `gh` and `GitLabTracker` to the fake `glab`, which `Harness.bin` holds on the PATH,
and the Target project's `origin` points at github.com but pushes to a bare repository next to it.
"""

import atexit
import contextlib
import functools
import io
import json
import os
import pickle
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import yaml

from factory.adapters import sbx, terminal_workbench
from factory.adapters.claude_code import ClaudeCodeExecutor
from factory.adapters.local_sandbox import LOCAL, LocalProcess, LocalSandbox
from factory.adapters.sbx import SbxSandbox
from factory.adapters.terminal_workbench import TerminalWorkbench
from factory.cli import main
from factory.config import Project, load_project
from factory.core import spawn
from factory.journal.recovery import replay
from factory.pipeline.library_lookup import Layer, Library
from factory.pipeline.throttle import WindowUsage
from factory.ports.executor import AgentKit, AttemptHandle, AttemptRequest, AttemptResult
from factory.ports.kind import Kind, Tools
from factory.ports.sandbox import Needs, Process, Sandbox, SandboxError
from factory.ports.workbench import Workbench
from factory.run.live import LiveTicket
from factory.run.orchestrator import Orchestrator
from factory.run.run_context import TICK_SECONDS
from factory.wiring import DEFAULT_EXECUTOR, DEFAULT_SANDBOX, DEFAULT_WORKBENCH, REGISTRIES, Machine, Registries
from fake_gh import INITIAL
from fake_glab import INITIAL as GLAB_INITIAL
from scripted_agent import gate_of, parked_marker

AGENT = Path(__file__).with_name("scripted_agent.py")
FAKE_SBX = Path(__file__).with_name("fake_sbx.py")
FAKE_GH = Path(__file__).with_name("fake_gh.py")
FAKE_GLAB = Path(__file__).with_name("fake_glab.py")
FAKE_ORCA = Path(__file__).with_name("fake_orca.py")
REPO = "owner/target"
# What a Ticket of a snapshot holds that these tests do not set.
TICKET_FIELDS: dict[str, list[str]] = {"blocked_by": [], "labels": [], "blocked_outside": []}
FACTORY_LIBRARY = Path(__file__).resolve().parents[1] / "src" / "factory" / "library"

# The Run view waits this long, in seconds, for a FakeTty to answer its probe.
# A FakeTty holds every answer a test types before the Run starts, so the probe reads it at once.
PROBE_SECONDS = 0.05

# `Harness.end` waits this long, in seconds, for the processes of a test to exit on their own.
# A process that ends at all ends well within it, even on a loaded machine.
EXIT_GRACE_SECONDS = 5

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
        "ci-fixer": {"prompt": "prompts/ci-fixer.md", "output": "schemas/ci-fixer.json"},
        "describer": {
            "prompt": "prompts/describer.md",
            "output": "schemas/describer.json",
            "skills": ["pr"],
            "body": yaml.safe_load((FACTORY_LIBRARY / "roles" / "describer.yaml").read_text())["body"],
        },
        "planner": {"prompt": "prompts/planner.md", "output": "schemas/planner.json", "selects": True},
    },
    "stages": {"implement": {"role": "implementer", "inputs": ["spec", "ticket"], "next": {"done": "catch_up"}}},
    "operations": {
        "catch_up": {"kind": "catch_up", "next": {"clean": "merge", "conflict": "abandoned"}},
        "merge": {"kind": "merge", "next": {"merged": "done"}},
    },
}


# The project file names the Pipeline of the Harness's config, which sits next to the Target project.
# The Run's sleep block is off unless a test asks for it, so no test starts a helper of the host.
# The Run opens its PR with the Tracker's text, so only a test that calls `with_describer` plays a Describer.
PROJECT = {
    "pipeline": {"file": "../config/pipeline.yaml"},
    "base": "main",
    "prevent_sleep": False,
    "run": "no-describer",
}

# The default Run graph without its Stage `describe`, which `PROJECT` names.
NO_DESCRIBER = {
    "start": "select",
    "escalate_to": "ended",
    "nodes": {
        "select": {"kind": "all", "next": {"selected": "build", "empty": "ended"}},
        "build": {
            "kind": "tickets",
            "with": {"pipeline": "${vars.pipeline}"},
            "next": {"all_merged": "open_pr", "some_merged": "open_pr", "none_merged": "ended"},
        },
        "open_pr": {"kind": "open_pr", "next": {"opened": "ci", "failed": "ended"}},
        "ci": {"kind": "wait_ci", "next": {"green": "ended", "absent": "ended", "accepted": "ended"}},
    },
}


def load_real_project(path: Path) -> Project:
    """Loads a project file of this repository with a Machine whose home is a fresh directory."""
    machine = Machine(home=Path(tempfile.mkdtemp()))
    return load_project(path, machine.library, machine.kinds)


class FakeClock:
    """FakeClock starts at a fixed instant and moves forward only when the Run sleeps.

    `on_sleep` runs before each sleep, so a test can look at a Run while it waits.
    A tick takes real time, so a process that works beside the Run gets the CPU.
    A tick takes no real time while `parked` says that only parked agents run beside the Run.
    A sleep longer than a tick takes no real time either, because only the Run's pause is that long,
    and the Run pauses only when nothing runs.
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
    `utilization` lists the share used of the five-hour window that the agent's rate limit events tell, in order.
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
    utilization: list[float] = field(default_factory=list)


def wait_until(ready: Callable[[], bool], seconds: float) -> bool:
    """Polls `ready` until it holds or `seconds` have passed, and returns whether it held."""
    deadline = time.monotonic() + seconds
    while not ready():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def group_ended(leader: Process) -> bool:
    """Returns whether every process of the group that `leader` leads is gone, such as the tools its agent started."""
    assert isinstance(leader, LocalProcess)
    listing = subprocess.run(["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, check=True).stdout
    return not any(
        int(pgid) == leader.pid and not stat.startswith("Z")
        for pgid, stat in (line.split() for line in listing.splitlines())
    )


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

    default_model = ClaudeCodeExecutor.default_model
    reported_windows = ClaudeCodeExecutor.reported_windows

    def settings_problem(self, model: str, effort: str | None) -> str | None:
        return ClaudeCodeExecutor().settings_problem(model, effort)

    def windows(self) -> dict[str, WindowUsage]:
        """Returns the usage the test injected, as an Executor that reports its windows would."""
        return dict(self.window_usage)

    name = ClaudeCodeExecutor.name

    def needs(self) -> Needs:
        return ClaudeCodeExecutor().needs()

    def reads(self) -> list[Path]:
        return ClaudeCodeExecutor().reads()

    def translation_version(self) -> str:
        return ClaudeCodeExecutor().translation_version()

    def assemble(self, kit: AgentKit, skills: Path, into: Path) -> None:
        ClaudeCodeExecutor().assemble(kit, skills, into)

    def start(self, request: AttemptRequest, sandbox: Sandbox) -> ScriptedHandle:
        played = sum(1 for e in self.requests if (e.sandbox, e.stage) == (request.sandbox, request.stage))
        self.requests.append(request)
        if self.on_start is not None:
            self.on_start(request)
        ticket = json.loads(request.brief.read_text()).get("ticket")
        scripts = self.ticket_scripts.get((ticket, request.stage)) or self.scripts[request.stage]
        script = scripts[min(played, len(scripts) - 1)] if isinstance(scripts, list) else scripts
        scenario = request.attempt_dir / "scenario.json"
        write_scenario(script, scenario)
        executor = ClaudeCodeExecutor(command=(sys.executable, str(AGENT), str(scenario)))
        return ScriptedHandle(executor.start(request, sandbox), request.stream)

    def launch(self, request: AttemptRequest) -> dict[str, Any]:
        """Returns the arguments, environment and stdin the scripted agent of `request` was started with."""
        launch: dict[str, Any] = json.loads((request.attempt_dir / "launch.json").read_text())
        return launch


def write_scenario(script: AgentScript, scenario: Path) -> None:
    """Writes the scenario file from which the scripted agent plays `script`."""
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
                "utilization": script.utilization,
            }
        )
    )


def scenario_of(argv: list[str]) -> Path | None:
    """Returns the scenario a scripted agent started with `argv` plays, or None for any other process."""
    return Path(argv[argv.index(str(AGENT)) + 1]) if str(AGENT) in argv else None


@dataclass(frozen=True)
class Parked:
    """Parked tells whether a scripted agent waits to be stopped, or waits for its `gate` file while it is not there.

    The agent leaves `marker` before it waits, and it sees its gate only on its next poll,
    so the clock looks for the gate itself.
    """

    marker: Path
    gate: Path | None

    def holds(self) -> bool:
        return self.marker.exists() and not (self.gate is not None and self.gate.exists())


class FlakySandbox(LocalSandbox):
    """FlakySandbox is a LocalSandbox that refuses to start the next `refuse` agents and `lost_probes` Probes.

    With `refuse_node` set it refuses only the agents of that node.
    It keeps every process it started in `started`, so a test can see whether one was stopped,
    and `parked` tells the clock whether the only ones still running, if any, are scripted agents that wait to be
    stopped or wait for a file that is not there yet.
    With `refuse_remove` set it also refuses to remove a Sandbox, and with `left_behind` set it reports that reason
    for a Sandbox that stays behind, which with `left_behind_sandbox` set is only that Sandbox.
    """

    def __init__(self) -> None:
        super().__init__()
        self.refuse = 0
        self.refuse_node: str | None = None
        self.lost_probes = 0
        self.refuse_remove = False
        self.left_behind: str | None = None
        self.left_behind_sandbox: str | None = None
        self.started: list[Process] = []
        self._parked: dict[Process, Parked] = {}

    def parked(self) -> bool:
        """Returns whether every process that still runs is a parked scripted agent."""
        running = [process for process in self.started if process.poll() is None]
        return all(process in self._parked and self._parked[process].holds() for process in running)

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
            self._parked[process] = Parked(parked_marker(scenario), gate_of(scenario))
        return process

    def remove(self, name: str) -> str | None:
        if self.refuse_remove:
            raise SandboxError(f"Sandbox {name} did not stop")
        if self.left_behind is not None and self.left_behind_sandbox in (None, name):
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
        self.command = (sys.executable, "-I", "-S", str(FAKE_SBX), str(root))

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
        labels: Sequence[str] = (),
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
            "labels": list(labels),
        }
        self.write(data)
        return f"https://github.com/{repo}/issues/{number}"

    def edit(self, number: int, **fields: Any) -> None:
        data = self.state()
        data["repos"][REPO]["issues"][str(number)].update(fields)
        self.write(data)

    def milestone(self, number: int, title: str, repo: str = REPO) -> None:
        """Opens a milestone of the repository."""
        data = self.state()
        data["repos"].setdefault(repo, {"issues": {}}).setdefault("milestones", []).append(
            {"number": number, "title": title}
        )
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
        labels: Sequence[str] = (),
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
            "labels": list(labels),
        }
        self.write(data)
        return f"https://{host}/{project}/-/issues/{iid}"

    def milestone(
        self, id: int, title: str, project: str = "group/sub/project", host: str = "gitlab.example.com"
    ) -> None:
        """Opens a milestone of the project."""
        data = self.state()
        found = data["hosts"].setdefault(host, {"projects": {}})["projects"].setdefault(project, {"items": {}})
        found.setdefault("milestones", []).append({"id": id, "title": title})
        self.write(data)

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

    The Run view asks `isatty` once per tick, and an Orca Workbench once more before its probe,
    so `type_later` can type between two ticks of the Run.
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


def wrap(bin: Path, name: str, *argv: str) -> None:
    """Puts a command called `name` in the directory `bin` that runs `argv`, as a host CLI of that name would."""
    script = bin / name
    script.write_text(f'#!/bin/sh\nexec {shlex.join(argv)} "$@"\n')
    script.chmod(0o755)


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
        self.spawned: list[subprocess.Popen[Any]] = []
        self.ran_only_check = True
        self._threads = set(threading.enumerate())
        self.clock.parked = self._parked
        self.sbx = FakeSbx(root / "sbx")
        self.remote = root / "remote.git"
        self.gh = FakeGh(root / "gh", self.remote)
        self.glab = FakeGlab(root / "glab", self.remote)
        self.bin = root / "bin"
        self.bin.mkdir()
        wrap(self.bin, "gh", *self.gh.command)
        wrap(self.bin, "glab", *self.glab.command)
        self.orca = FakeOrca(root / "orca")
        self.workbench = LingeringWorkbench()
        self.workbench_override: Workbench | None = None
        self.registries = self._registries()
        self.pipeline: dict[str, Any] = json.loads(json.dumps(PIPELINE))
        self.pipeline_file = self.config / "pipeline.yaml"
        self.project_file = self.repo / "factory.yaml"
        self.project: dict[str, Any] = dict(PROJECT)
        self.machine_config: dict[str, Any] = {}
        self.factory_library = root / "factory-library"
        shutil.copytree(FACTORY_LIBRARY / "runs", self.factory_library / "runs")
        (self.factory_library / "runs" / "no-describer.yaml").write_text(yaml.safe_dump(NO_DESCRIBER))
        self.keep_roles = False
        self.detach_remotes = True
        self.stdin: Any = None
        self.platform = sys.platform
        self.orphans: set[int] = set()
        self._init_config()
        self._init_repo()
        self._write_config()

    def _parked(self) -> bool:
        """Tells the clock whether only parked agents run beside the Run.

        An agent is parked while it waits to be stopped, or waits for a `wait_for` file that is not there yet.
        A test makes that file from `on_sleep`, from a thread, from a process it spawned or from a Sandbox process,
        and the clock sees each of them work.
        The clock cannot see the processes of an SbxSandbox, so it never takes them for parked agents.
        """
        if self.sbx_used or any(process.poll() is None for process in self.spawned):
            return False
        if any(thread.is_alive() for thread in set(threading.enumerate()) - self._threads):
            return False
        return self.sandbox.parked()

    def _init_repo(self) -> None:
        """Copies the Target project and its remote from the template and points `origin` pushes at the remote.

        It writes the rewrites of the remote URLs straight into the git config, as `git config --add` would.
        """
        template = repo_template()
        shutil.copytree(template / "target", self.repo, symlinks=True)
        shutil.copytree(template / "remote.git", self.remote, symlinks=True)
        urls = (
            f"git@github.com:{REPO}.git",
            "git@gitlab.example.com:group/sub/project.git",
            "https://gitlab.com/group/sub/project.git",
        )
        with (self.repo / ".git" / "config").open("a") as config:
            config.write(f'[url "{self.remote}"]\n' + "".join(f"\tinsteadOf = {url}\n" for url in urls))

    def _init_config(self) -> None:
        (self.config / "prompts").mkdir(parents=True)
        (self.config / "schemas").mkdir()
        (self.config / "prompts" / "implementer.md").write_text("Implement the Ticket.\n")
        (self.config / "schemas" / "implementer.json").write_text(json.dumps(IMPLEMENTER_SCHEMA))
        library = Library([Layer("factory", FACTORY_LIBRARY)])
        for role in ("ci-fixer", "describer", "planner", "reviewer"):
            (self.config / "prompts" / f"{role}.md").write_text(library.prompt(role).text)
            (self.config / "schemas" / f"{role}.json").write_text(json.dumps(library.schema(role).document, indent=2))

    def lose_cli(self, name: str) -> None:
        """Takes the host CLI `name` off the PATH, its fake and a real one alike, as on a machine that lacks it.

        Each other directory that holds it gives way to a directory of links to all it holds but that CLI.
        The `factory` fixture puts the PATH back after the test.
        """
        (self.bin / name).unlink()
        kept = []
        for index, entry in enumerate(os.environ["PATH"].split(os.pathsep)):
            if not (Path(entry) / name).exists():
                kept.append(entry)
                continue
            links = self.root / "path" / str(index)
            links.mkdir(parents=True)
            for tool in Path(entry).iterdir():
                if tool.name != name:
                    (links / tool.name).symlink_to(tool)
            kept.append(str(links))
        os.environ["PATH"] = os.pathsep.join(kept)

    def remove_remotes(self) -> None:
        """Takes the git remotes off the Target project, so that a Run from a Spec folder records its PR there.

        While the project has no remote-tracking refs and no branch config, a remote is only its section of the
        git config, so it drops those sections itself, and otherwise it leaves the work to `git remote remove`.
        """
        dot_git = self.repo / ".git"
        config = dot_git / "config"
        packed = dot_git / "packed-refs"
        if (
            (dot_git / "refs" / "remotes").exists()
            or (packed.exists() and "refs/remotes/" in packed.read_text())
            or '[branch "' in config.read_text()
        ):
            for name in git(self.repo, "remote").split():
                git(self.repo, "remote", "remove", name)
            return
        sections = re.split(r"^(?=\[)", config.read_text(), flags=re.MULTILINE)
        config.write_text("".join(section for section in sections if not section.startswith('[remote "')))

    def write_spec(
        self, title: str, body: str, tickets: dict[str, str], labels: Mapping[str, list[str]] | None = None
    ) -> Path:
        """Writes a Spec folder whose Tickets carry the `labels` given them, and unless `detach_remotes` is off the
        project loses its git remotes."""
        if self.detach_remotes:
            self.remove_remotes()
        spec = self.tracker / "spec"
        (spec / "tickets").mkdir(parents=True)
        (spec / "spec.md").write_text(f"# {title}\n\n{body}\n")
        for ticket_id, text in tickets.items():
            (spec / "tickets" / f"{ticket_id}.md").write_text(text)
            if carried := (labels or {}).get(ticket_id):
                (spec / "projection" / ticket_id).mkdir(parents=True)
                (spec / "projection" / ticket_id / "labels").write_text("".join(f"{label}\n" for label in carried))
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
            if "body" in role:
                data["body"] = role["body"]
            if "selects" in role:
                data["selects"] = role["selects"]
            (library / "roles" / f"{name}.yaml").write_text(yaml.safe_dump(data))
        self.pipeline_file.write_text(yaml.safe_dump(pipeline))
        self.project_file.write_text(yaml.safe_dump(self.project))

    def machine(self, clock: FakeClock | None = None) -> Machine:
        """Returns the Machine the CLI runs with, after writing the factory config of `machine_config`."""
        self.ran_only_check = False
        self._write_config()
        if self.machine_config:
            self.home.mkdir(exist_ok=True)
            (self.home / "config.yaml").write_text(yaml.safe_dump(self.machine_config))
        return Machine(
            home=self.home,
            clock=clock or self.clock,
            registries=self.registries,
            audit=self._audit,
            platform=self.platform,
            factory_library=self.factory_library,
            **({"stdin": self.stdin} if self.stdin else {}),
        )

    def _audit(self, orchestrator: Orchestrator) -> None:
        """Replays the journal of a Run that ended and asserts that the result equals the Run's live state."""
        tickets = [LiveTicket(id=t.id, blocked_by=t.blocked_by) for t in orchestrator.context.snapshot.tickets]
        events = orchestrator.journal.events(orchestrator.run_id)
        project = orchestrator.project
        replayed = replay(
            orchestrator.pipeline,
            tickets,
            events,
            project.max_parallel,
            project.throttle,
            project.budgets,
            executor=project.executor.kind,
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
        return self._track(
            subprocess.Popen(
                [sys.executable, "-c", code, self.clock.now().isoformat(), *argv],
                env=self._spawn_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )

    def spawn_run(self, spec: Path | str) -> "subprocess.Popen[bytes]":
        """Starts `factory run` of `spec` as a process of its own, whose agents play the scripts of `executor`.

        The process holds a FrozenClock at the instant of this one, so its Run waits in real time,
        and it writes its output to `spawned-run.log` in the root.
        """
        machine = self.machine()
        setup = self.root / "spawned-run.pickle"
        setup.write_bytes(
            pickle.dumps(
                Spawned(
                    self.clock.now(),
                    self.executor,
                    machine.home,
                    machine.platform,
                    machine.factory_library,
                )
            )
        )
        code = "import sys; from harness import run_spawned; sys.exit(run_spawned(sys.argv[1:]))"
        argv = [sys.executable, "-c", code, str(setup), "run", str(spec), "--project", str(self.project_file)]
        with (self.root / "spawned-run.log").open("wb") as log:
            return self._track(subprocess.Popen(argv, env=self._spawn_env(), stdout=log, stderr=log))

    def _track[P: subprocess.Popen[Any]](self, process: P) -> P:
        """Keeps a process the test spawned, so the clock never takes the Run for idle while it works."""
        self.spawned.append(process)
        return process

    def _spawn_env(self) -> dict[str, str]:
        return {
            **os.environ,
            "FACTORY_HOME": str(self.home),
            "PYTHONPATH": os.pathsep.join([str(Path(__file__).parent), os.environ.get("PYTHONPATH", "")]),
        }

    def cli(self, *argv: str) -> CliResult:
        """Runs `factory` with `argv` in this process, and with a FakeTty on stdin the probe waits `PROBE_SECONDS`."""
        ran_only_check = self.ran_only_check and argv[:1] == ("check",)
        machine = self.machine()
        self.ran_only_check = ran_only_check
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            if isinstance(self.stdin, FakeTty):
                stack.enter_context(patch.object(terminal_workbench, "PROBE_SECONDS", PROBE_SECONDS))
            code = main(list(argv), machine)
        return CliResult(code, out.getvalue(), err.getvalue())

    def _registries(self) -> Registries:
        """Returns the registries of the Machine, with the fakes of this Harness as the kinds of the real names.

        The Executor kind makes the ScriptedExecutor, the kind `local` the FlakySandbox,
        the kind `sbx` an SbxSandbox on the fake `sbx`, and every Workbench kind the `workbench_override` if a test
        set one, else the LingeringWorkbench.
        A test registers more kinds on them.
        """
        registries = with_executor(REGISTRIES, self.executor)
        registries.sandboxes.register(DEFAULT_SANDBOX, Kind(lambda settings, tools: self.sandbox, parse=LOCAL.parse))
        registries.sandboxes.register("sbx", Kind(self._sbx, sbx.KIND.fields, sbx.KIND.parse, enforces_egress=True))
        for name in (DEFAULT_WORKBENCH, "orca"):
            registries.workbenches.register(
                name, Kind(lambda settings, tools: self.workbench_override or self.workbench)
            )
        return registries

    def _sbx(self, settings: sbx.SbxSettings, tools: Tools) -> Sandbox:
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

    def processes(self) -> dict[int, str]:
        """Returns the command of every process this test started that still runs, by pid.

        These are the descendants of the test's process and every process whose command names the test's root,
        which finds a renderer or an agent whose factory process died too.
        """
        lister = subprocess.Popen(["ps", "-axww", "-o", "pid=,ppid=,stat=,command="], stdout=subprocess.PIPE, text=True)
        listing, _ = lister.communicate()
        children: dict[int, list[int]] = {}
        commands: dict[int, str] = {}
        for line in listing.splitlines():
            pid, ppid, stat, command = line.split(maxsplit=3)
            if int(pid) != lister.pid and not stat.startswith("Z"):
                children.setdefault(int(ppid), []).append(int(pid))
                commands[int(pid)] = command
        found = {pid for pid, command in commands.items() if f"{self.root}{os.sep}" in command}
        stack = [os.getpid(), *found]
        while stack:
            for child in children.get(stack.pop(), []):
                if child not in found:
                    found.add(child)
                    stack.append(child)
        return {pid: commands[pid] for pid in sorted(found)}

    def crashed(self) -> None:
        """Marks every process that runs now as left by a Run whose process died, which the test need not end."""
        self.orphans |= self.processes().keys()

    def end(self) -> dict[int, str]:
        """Kills every process the test left running, and returns those that a correct Run would have ended.

        A process may still be exiting when the test ends, so `end` gives it a moment first.
        A test whose every Machine ran `factory check` and that spawned nothing and started no Sandbox process
        leaves none, so `end` lists no processes for it.
        """
        if self.ran_only_check and not self.spawned and not self.sandbox.started and not self.sbx_used:
            return {}
        left: dict[int, str] = {}

        def settled() -> bool:
            nonlocal left
            left = self.processes()
            return not left.keys() - self.orphans

        wait_until(settled, EXIT_GRACE_SECONDS)
        for pid in left:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        for pid in left:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(pid, 0)
        return {pid: command for pid, command in left.items() if pid not in self.orphans}

    def all_events(self, run_id: str) -> list[tuple[str, dict[str, Any]]]:
        with contextlib.closing(sqlite3.connect(self.home / "factory.db")) as db:
            rows = db.execute(
                "SELECT type, payload FROM events WHERE run_id = ? AND type != 'Heartbeat' ORDER BY seq", (run_id,)
            )
            return [(type, json.loads(payload)) for (type, payload) in rows]


@dataclass
class Spawned:
    """Spawned is what `Harness.spawn_run` hands the process it starts, to build the Machine of its Run."""

    now: datetime
    executor: ScriptedExecutor
    home: Path
    platform: str
    factory_library: Path


def run_spawned(argv: list[str]) -> int:
    """Runs `factory` with `argv[1:]` in a process that `Harness.spawn_run` started, on the setup in `argv[0]`."""
    spawned: Spawned = pickle.loads(Path(argv[0]).read_bytes())
    machine = Machine(
        home=spawned.home,
        clock=FrozenClock(spawned.now),
        registries=with_executor(REGISTRIES, spawned.executor),
        platform=spawned.platform,
        factory_library=spawned.factory_library,
    )
    return main(argv[1:], machine)


def with_executor(registries: Registries, executor: "ScriptedExecutor") -> Registries:
    """Returns a copy of `registries` whose Executor kind makes `executor`."""
    copy = registries.copy()
    copy.executors.register(DEFAULT_EXECUTOR, Kind(lambda settings, tools: executor))
    return copy


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
