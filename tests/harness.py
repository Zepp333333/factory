"""Seam 1 harness: the whole `factory` CLI on a temporary Target project.

Only two ports are replaced.
`ScriptedExecutor` plays the agent from a per-Stage script, and `LocalSandbox` runs it straight in the Workspace.
SQLite, host git, `TerminalWorkbench`, `LocalTracker` and Config stay real, and the clock is injected.
"""

import contextlib
import io
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from factory.cli import Context, main
from factory.executor import AttemptRequest, AttemptResult
from factory.sandbox import Box, LocalSandbox, Process
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
    "defaults": {"model": "sonnet"},
    "roles": {"implementer": {"prompt": "prompts/implementer.md", "output": "schemas/implementer.json"}},
    "stages": {"implement": {"role": "implementer", "inputs": ["spec", "ticket"], "next": {"done": "merge"}}},
    "operations": {"merge": {"kind": "merge", "next": {"merged": "done"}}},
}


class FakeClock:
    """Starts at a fixed instant and moves forward only when the Run sleeps."""

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
    """What the scripted agent does in one Attempt: the files it writes and the JSON it returns."""

    output: dict[str, Any]
    files: dict[str, str] = field(default_factory=dict)


class ScriptedHandle:
    def __init__(self, process: Process, stream: Path) -> None:
        self._process = process
        self._stream = stream

    def poll(self) -> AttemptResult | None:
        code = self._process.poll()
        if code is None:
            return None
        events = [json.loads(line) for line in self._stream.read_text().splitlines() if line.strip()]
        result = next((e for e in events if e.get("type") == "result"), None)
        if code != 0 or result is None or result.get("is_error"):
            return AttemptResult(output=None, failure="executor_crash")
        return AttemptResult(output=result["structured_output"], failure=None)


class ScriptedExecutor:
    def __init__(self) -> None:
        self.scripts: dict[str, AgentScript] = {}
        self.requests: list[AttemptRequest] = []

    def start(self, request: AttemptRequest, box: Box) -> ScriptedHandle:
        self.requests.append(request)
        script = self.scripts[request.stage]
        scenario = request.attempt_dir / "scenario.json"
        scenario.write_text(json.dumps({"model": request.model, "files": script.files, "output": script.output}))
        process = box.exec([sys.executable, str(AGENT), str(scenario)], stdout=request.stream)
        return ScriptedHandle(process, request.stream)


@dataclass
class CliResult:
    code: int
    out: str


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
        self.pipeline: dict[str, Any] = json.loads(json.dumps(PIPELINE))
        self.project_file = self.repo / "factory.yaml"
        self._init_config()
        self._init_repo()

    def _init_repo(self) -> None:
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "Owner")
        git(self.repo, "config", "user.email", "owner@example.com")
        (self.repo / "README.md").write_text("# Target\n")
        self.project_file.write_text(yaml.safe_dump({"pipeline": str(self.config / "pipeline.yaml"), "base": "main"}))
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

    def cli(self, *argv: str) -> CliResult:
        (self.config / "pipeline.yaml").write_text(yaml.safe_dump(self.pipeline))
        context = Context(
            home=self.home,
            clock=self.clock,
            executor=self.executor,
            sandbox=LocalSandbox(),
            workbench=TerminalWorkbench(),
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(list(argv), context)
        return CliResult(code, out.getvalue())

    def run(self, spec: Path) -> CliResult:
        return self.cli("run", str(spec), "--project", str(self.project_file))

    def git(self, *args: str) -> str:
        return git(self.repo, *args)

    def branches(self) -> dict[str, str]:
        lines = self.git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads").splitlines()
        return dict(line.split(" ", 1) for line in lines)

    def run_dir(self, run_id: str) -> Path:
        return self.home / "runs" / run_id


def run_id_of(out: str) -> str:
    match = re.search(r"^Run (\S+) started", out, re.MULTILINE)
    assert match, out
    return match.group(1)
