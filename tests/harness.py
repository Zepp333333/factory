"""The Seam 1 harness drives the whole `factory` CLI on a temporary Target project.

Only two ports are replaced.
`ScriptedExecutor` runs the real `ClaudeCodeExecutor` on an agent that plays a per-Stage script,
and `LocalSandbox` runs it straight in the Workspace.
SQLite, host git, `TerminalWorkbench`, `LocalTracker`, `GitHubTracker` and Config stay real, and the clock is injected.
`GitHubTracker` talks to the fake `gh`, and the Target project's `origin` points at github.com
but pushes to a bare repository next to it.
"""

import contextlib
import io
import json
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml

from factory.claude_code import ClaudeCodeExecutor
from factory.cli import Machine, main
from factory.executor import AttemptHandle, AttemptRequest, AttemptResult
from factory.sandbox import LocalSandbox, Process, Sandbox, SandboxError, SandboxSettings
from factory.sbx import SbxSandbox
from factory.workbench import TerminalWorkbench
from fake_gh import INITIAL

AGENT = Path(__file__).with_name("scripted_agent.py")
FAKE_SBX = Path(__file__).with_name("fake_sbx.py")
FAKE_GH = Path(__file__).with_name("fake_gh.py")
REPO = "owner/target"
SELF_PIPELINE = Path(__file__).resolve().parents[1] / "pipelines" / "self"
RESETS_AT = datetime(2026, 10, 20, 13, 0, 0, tzinfo=UTC)

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
    "stages": {"implement": {"role": "implementer", "inputs": ["spec", "ticket"], "next": {"done": "merge"}}},
    "operations": {"merge": {"kind": "merge", "next": {"merged": "done"}}},
}


class FakeClock:
    """FakeClock starts at a fixed instant and moves forward only when the Run sleeps.

    `on_sleep` runs before each sleep, so a test can look at a Run while it waits.
    """

    def __init__(self) -> None:
        self._now = datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)
        self.on_sleep: Callable[[float], None] | None = None

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        if self.on_sleep is not None:
            self.on_sleep(seconds)
        self._now += timedelta(seconds=seconds)

        # The scripted agent is a real process, so the Run still yields the CPU to it.
        time.sleep(min(seconds, 0.005))


@dataclass
class AgentScript:
    """An AgentScript lists the files the scripted agent writes in one Attempt and the JSON it returns.

    The agent first deletes the paths in `remove`, then writes `files` and runs the shell commands in `run`.
    `fail` makes the agent `crash`, `hang`, fail `auth`, get `rate_limited` or run out of turns with `max_turns`
    after it writes its files.
    A `rate_limited` agent says the limit resets at `resets_at`, or gives no reset time when it is None.
    `leak` adds plugins, skills, agents or MCP servers outside the factory's set to the agent's init event.
    """

    output: dict[str, Any] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    remove: list[str] = field(default_factory=list)
    run: list[str] = field(default_factory=list)
    fail: Literal["crash", "hang", "auth", "rate_limited", "max_turns"] | None = None
    resets_at: datetime | None = RESETS_AT
    leak: dict[str, list[str]] = field(default_factory=dict)


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
    A script in `ticket_scripts` under a Ticket and a Stage plays instead for that Ticket.
    """

    def __init__(self) -> None:
        self.scripts: dict[str, AgentScript | list[AgentScript]] = {}
        self.ticket_scripts: dict[tuple[str, str], AgentScript | list[AgentScript]] = {}
        self.requests: list[AttemptRequest] = []

    def reads(self) -> list[Path]:
        return ClaudeCodeExecutor().reads()

    def start(self, request: AttemptRequest, sandbox: Sandbox) -> ScriptedHandle:
        played = sum(1 for e in self.requests if (e.sandbox, e.stage) == (request.sandbox, request.stage))
        self.requests.append(request)
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
                }
            )
        )
        executor = ClaudeCodeExecutor(command=(sys.executable, str(AGENT), str(scenario)))
        return ScriptedHandle(executor.start(request, sandbox), request.stream)

    def launch(self, request: AttemptRequest) -> dict[str, Any]:
        """Returns the arguments, environment and stdin the scripted agent of `request` was started with."""
        launch: dict[str, Any] = json.loads((request.attempt_dir / "launch.json").read_text())
        return launch


class FlakySandbox(LocalSandbox):
    """FlakySandbox is a LocalSandbox that refuses to start the next `refuse` agents and `lost_probes` Probes.

    With `refuse_remove` set it also refuses to remove a Sandbox.
    """

    def __init__(self) -> None:
        super().__init__()
        self.refuse = 0
        self.lost_probes = 0
        self.refuse_remove = False

    def exec(
        self, name: str, argv: list[str], stdout: Path, env: dict[str, str] | None = None, stderr: Path | None = None
    ) -> Process:
        probe = env is not None and "GATE_OUT" in env
        if probe and self.lost_probes:
            self.lost_probes -= 1
            raise SandboxError(f"Sandbox {name} did not start")
        if not probe and self.refuse:
            self.refuse -= 1
            raise SandboxError(f"Sandbox {name} did not start")
        return super().exec(name, argv, stdout, env, stderr)

    def remove(self, name: str) -> str | None:
        if self.refuse_remove:
            raise SandboxError(f"Sandbox {name} did not stop")
        return super().remove(name)


class LingeringWorkbench(TerminalWorkbench):
    """LingeringWorkbench is a TerminalWorkbench with a process left behind in the Sandbox.

    The process writes `late_files` into the Workspace right before the Workbench removes it.
    """

    def __init__(self) -> None:
        self.late_files: dict[str, str] = {}

    def remove_workspace(self, repo: Path, path: Path) -> None:
        for name, text in self.late_files.items():
            (path / name).write_text(text)
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

    def edit(self, number: int, **fields: str) -> None:
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

    def pulls(self) -> list[dict[str, Any]]:
        pulls: list[dict[str, Any]] = self.state()["pulls"]
        return pulls

    def state(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads((self.root / "state.json").read_text())
        return data

    def write(self, data: dict[str, Any]) -> None:
        (self.root / "state.json").write_text(json.dumps(data))


@dataclass
class CliResult:
    code: int
    out: str
    err: str = ""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


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
        self.sbx = FakeSbx(root / "sbx")
        self.remote = root / "remote.git"
        self.gh = FakeGh(root / "gh", self.remote)
        self.workbench = LingeringWorkbench()
        self.pipeline: dict[str, Any] = json.loads(json.dumps(PIPELINE))
        self.pipeline_file = self.config / "pipeline.yaml"
        self.project_file = self.repo / "factory.yaml"
        self.project: dict[str, Any] = {"pipeline": str(self.pipeline_file), "base": "main"}
        self.machine_config: dict[str, Any] = {}
        self._init_config()
        self._init_repo()

    def _init_repo(self) -> None:
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "Owner")
        git(self.repo, "config", "user.email", "owner@example.com")
        (self.repo / "README.md").write_text("# Target\n")
        self._write_config()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial")
        git(self.repo, "branch", "feature")
        git(self.root, "init", "-q", "--bare", str(self.remote))
        git(self.repo, "remote", "add", "origin", f"git@github.com:{REPO}.git")
        git(self.repo, "config", f"url.{self.remote}.insteadOf", f"git@github.com:{REPO}.git")

    def _init_config(self) -> None:
        (self.config / "prompts").mkdir(parents=True)
        (self.config / "schemas").mkdir()
        (self.config / "prompts" / "implementer.md").write_text("Implement the Ticket.\n")
        (self.config / "schemas" / "implementer.json").write_text(json.dumps(IMPLEMENTER_SCHEMA))
        for name, path in (
            ("prompts/describer.md", "prompts/describer.md"),
            ("schemas/describer.json", "schemas/describer.json"),
        ):
            (self.config / name).write_text((SELF_PIPELINE / path).read_text())

    def write_spec(self, title: str, body: str, tickets: dict[str, str]) -> Path:
        spec = self.tracker / "spec"
        (spec / "tickets").mkdir(parents=True)
        (spec / "spec.md").write_text(f"# {title}\n\n{body}\n")
        for ticket_id, text in tickets.items():
            (spec / "tickets" / f"{ticket_id}.md").write_text(text)
        return spec

    def _write_config(self) -> None:
        self.pipeline_file.write_text(yaml.safe_dump(self.pipeline))
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
            workbench=self.workbench,
            gh=self.gh.command,
        )

    def cli(self, *argv: str) -> CliResult:
        machine = self.machine()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv), machine)
        return CliResult(code, out.getvalue(), err.getvalue())

    def _sandbox(self, settings: SandboxSettings) -> Sandbox:
        """Gives a project with `sandbox.kind: sbx` an SbxSandbox on the fake `sbx`, and any other the FlakySandbox."""
        return SbxSandbox(settings, command=self.sbx.command) if settings.kind == "sbx" else self.sandbox

    def run(self, spec: Path | str, *flags: str) -> CliResult:
        return self.cli("run", str(spec), "--project", str(self.project_file), *flags)

    def remote_branches(self) -> dict[str, str]:
        lines = git(self.remote, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").splitlines()
        return dict(line.split(" ", 1) for line in lines)

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

    def all_runs_events(self) -> list[tuple[str, str, dict[str, Any]]]:
        """Returns the run id, type and payload of every event of every Run, in the order they were journaled."""
        with contextlib.closing(sqlite3.connect(self.home / "factory.db")) as db:
            rows = db.execute("SELECT run_id, type, payload FROM events ORDER BY seq")
            return [(run_id, type, json.loads(payload)) for (run_id, type, payload) in rows]

    def all_events(self, run_id: str) -> list[tuple[str, dict[str, Any]]]:
        with contextlib.closing(sqlite3.connect(self.home / "factory.db")) as db:
            rows = db.execute("SELECT type, payload FROM events WHERE run_id = ? ORDER BY seq", (run_id,))
            return [(type, json.loads(payload)) for (type, payload) in rows]


def run_id_of(out: str) -> str:
    match = re.search(r"^Run (\S+) started", out, re.MULTILINE)
    assert match, out
    return match.group(1)
