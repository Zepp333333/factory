"""The Seam 1 harness drives the whole `factory` CLI on a temporary Target project.

Only two ports are replaced.
`ScriptedExecutor` plays the agent from a per-Stage script, and `LocalSandbox` runs it straight in the Workspace.
SQLite, host git, `TerminalWorkbench`, `LocalTracker` and Config stay real, and the clock is injected.
"""

import contextlib
import io
import json
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml

from factory.cli import Machine, main
from factory.executor import AttemptRequest, AttemptResult
from factory.sandbox import LocalSandbox, Process, Sandbox, SandboxError
from factory.workbench import TerminalWorkbench

AGENT = Path(__file__).with_name("scripted_agent.py")

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
    "roles": {"implementer": {"prompt": "prompts/implementer.md", "output": "schemas/implementer.json"}},
    "stages": {"implement": {"role": "implementer", "inputs": ["spec", "ticket"], "next": {"done": "merge"}}},
    "operations": {"merge": {"kind": "merge", "next": {"merged": "done"}}},
}


class FakeClock:
    """FakeClock starts at a fixed instant and moves forward only when the Run sleeps."""

    def __init__(self) -> None:
        self._now = datetime(2026, 10, 20, 9, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

        # The scripted agent is a real process, so the Run still yields the CPU to it.
        time.sleep(min(seconds, 0.005))


@dataclass
class AgentScript:
    """An AgentScript lists the files the scripted agent writes in one Attempt and the JSON it returns.

    The agent first deletes the paths in `remove`, then writes `files` and runs the shell commands in `run`.
    `fail` makes the agent `crash`, `hang` or fail `auth` after it writes its files.
    """

    output: dict[str, Any] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)
    remove: list[str] = field(default_factory=list)
    run: list[str] = field(default_factory=list)
    fail: Literal["crash", "hang", "auth"] | None = None


class ScriptedHandle:
    """ScriptedHandle reads the scripted agent's stream once the agent has written its files.

    The agent prints its init event only after its files are on disk, so the fake clock never
    times out an Attempt that has not started yet.
    """

    def __init__(self, process: Process, stream: Path) -> None:
        self._process = process
        self._stream = stream
        self._started = False

    def poll(self) -> AttemptResult | None:
        deadline = time.monotonic() + 10
        while not self._started and self._process.poll() is None and time.monotonic() < deadline:
            self._started = self._stream.stat().st_size > 0
            time.sleep(0.005)
        code = self._process.poll()
        if code is None:
            return None
        events = [json.loads(line) for line in self._stream.read_text().splitlines() if line.strip()]
        result = next((e for e in events if e.get("type") == "result"), None)
        if result is not None and result.get("api_error_status") == 401:
            return AttemptResult(output=None, failure="auth")
        if code != 0 or result is None or result.get("is_error"):
            return AttemptResult(output=None, failure="executor_crash")
        return AttemptResult(output=result["structured_output"], failure=None)

    def kill(self) -> None:
        self._process.kill()


class ScriptedExecutor:
    """ScriptedExecutor plays each Stage from its script, or from a list taken one per Attempt of a Ticket."""

    def __init__(self) -> None:
        self.scripts: dict[str, AgentScript | list[AgentScript]] = {}
        self.requests: list[AttemptRequest] = []

    def start(self, request: AttemptRequest, sandbox: Sandbox) -> ScriptedHandle:
        played = sum(1 for e in self.requests if (e.sandbox, e.stage) == (request.sandbox, request.stage))
        self.requests.append(request)
        scripts = self.scripts[request.stage]
        script = scripts[min(played, len(scripts) - 1)] if isinstance(scripts, list) else scripts
        scenario = request.attempt_dir / "scenario.json"
        scenario.write_text(
            json.dumps(
                {
                    "model": request.model,
                    "remove": script.remove,
                    "files": script.files,
                    "run": script.run,
                    "output": script.output,
                    "fail": script.fail,
                }
            )
        )
        process = sandbox.exec(request.sandbox, [sys.executable, str(AGENT), str(scenario)], stdout=request.stream)
        return ScriptedHandle(process, request.stream)


class FlakySandbox(LocalSandbox):
    """FlakySandbox is a LocalSandbox that refuses to start the next `refuse` agents and `lost_probes` Probes."""

    def __init__(self) -> None:
        super().__init__()
        self.refuse = 0
        self.lost_probes = 0

    def exec(self, name: str, argv: list[str], stdout: Path, env: dict[str, str] | None = None) -> Process:
        probe = env is not None and "GATE_OUT" in env
        if probe and self.lost_probes:
            self.lost_probes -= 1
            raise SandboxError(f"Sandbox {name} did not start")
        if not probe and self.refuse:
            self.refuse -= 1
            raise SandboxError(f"Sandbox {name} did not start")
        return super().exec(name, argv, stdout, env)


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
        self.pipeline: dict[str, Any] = json.loads(json.dumps(PIPELINE))
        self.pipeline_file = self.config / "pipeline.yaml"
        self.project_file = self.repo / "factory.yaml"
        self.project: dict[str, Any] = {"pipeline": str(self.pipeline_file), "base": "main"}
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

    def _init_config(self) -> None:
        (self.config / "prompts").mkdir(parents=True)
        (self.config / "schemas").mkdir()
        (self.config / "prompts" / "implementer.md").write_text("Implement the Ticket.\n")
        (self.config / "schemas" / "implementer.json").write_text(json.dumps(IMPLEMENTER_SCHEMA))

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

    def cli(self, *argv: str) -> CliResult:
        self._write_config()
        machine = Machine(
            home=self.home,
            clock=self.clock,
            executor=self.executor,
            sandbox=self.sandbox,
            workbench=TerminalWorkbench(),
        )
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv), machine)
        return CliResult(code, out.getvalue(), err.getvalue())

    def run(self, spec: Path) -> CliResult:
        return self.cli("run", str(spec), "--project", str(self.project_file))

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


def run_id_of(out: str) -> str:
    match = re.search(r"^Run (\S+) started", out, re.MULTILINE)
    assert match, out
    return match.group(1)
