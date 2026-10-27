"""Seam 3 contract tests check that the real `sbx` behaves as SbxSandbox and the fake `sbx` assume.

They run only with FACTORY_CONTRACT=1, on a machine where `sbx` has the anthropic OAuth from a `/login`
and its global policy denies unlisted hosts.
All tests share one Sandbox on a throwaway repository, which the module removes at the end.
"""

import json
import os
import secrets
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from factory.claude_code import AgentSet, ClaudeCodeExecutor, Kit
from factory.executor import AttemptRequest
from factory.orchestrator import describer_names
from factory.sandbox import Mount, SandboxLost, SandboxSettings
from factory.sbx import SbxSandbox

pytestmark = pytest.mark.skipif(os.environ.get("FACTORY_CONTRACT") != "1", reason="set FACTORY_CONTRACT=1")


@dataclass
class Bench:
    sandbox: SbxSandbox
    name: str
    repo: Path
    workspace: Path
    out: Path


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Bench]:
    root = tmp_path_factory.mktemp("sbx")
    repo, workspace, out = root / "repo", root / "workspace", root / "out"
    repo.mkdir()
    out.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    git(repo, "worktree", "add", "-q", "-b", "ticket", str(workspace))
    sandbox = SbxSandbox(SandboxSettings(kind="sbx", cpus=2, memory="2g", egress=("pypi.org",)))
    sandbox.check()
    name = f"factory-contract-{secrets.token_hex(3)}"
    mounts = [Mount(repo / ".git"), Mount(out, writable=True), *(Mount(path) for path in ClaudeCodeExecutor().reads())]
    sandbox.create(name, workspace, mounts)
    try:
        yield Bench(sandbox, name, repo, workspace, out)
    finally:
        sandbox.remove(name)


def run(bench: Bench, script: str, timeout: float = 120) -> tuple[int, str]:
    """Runs a shell script in the Sandbox and returns its exit code and output."""
    return run_in(bench, bench.name, script, timeout)


def run_in(bench: Bench, name: str, script: str, timeout: float = 120) -> tuple[int, str]:
    """Runs a shell script in the Sandbox `name` and returns its exit code and output."""
    log = bench.out / f"{secrets.token_hex(3)}.txt"
    process = bench.sandbox.exec(name, ["sh", "-c", script], stdout=log, stderr=log.with_suffix(".err"))
    deadline = time.monotonic() + timeout
    while (code := process.poll()) is None:
        assert time.monotonic() < deadline, f"no exit after {timeout}s"
        time.sleep(0.1)
    return code, log.read_text()


def test_git_reads_the_read_only_shared_git_and_cannot_commit(bench: Bench) -> None:
    code, out = run(bench, "git status --short && git log --oneline -1 && git diff --stat")
    assert code == 0, out
    assert out.strip().endswith("base")

    code, out = run(bench, "git -c user.name=a -c user.email=a@a commit --allow-empty -m agent 2>&1")
    assert code != 0
    assert "Read-only file system" in out


def test_read_only_git_sees_a_host_commit_at_once(bench: Bench) -> None:
    git(bench.workspace, "commit", "-q", "--allow-empty", "-m", "host")
    head = git(bench.workspace, "rev-parse", "HEAD")

    code, out = run(bench, "git rev-parse HEAD")

    assert (code, out.strip()) == (0, head)


def test_egress_allows_the_claude_api_and_the_project_hosts_and_denies_the_rest(bench: Bench) -> None:
    status = 'curl -s -o /dev/null --max-time 30 -w "%{http_code}\\n"'
    code, out = run(
        bench, f"{status} https://api.anthropic.com/; {status} https://pypi.org/simple/; {status} https://example.com/"
    )

    anthropic, pypi, other = out.split()
    assert code == 0
    assert anthropic not in ("000", "403")
    assert pypi == "200"
    assert other == "403"


def test_command_exit_code_comes_back_through_its_file(bench: Bench) -> None:
    assert run(bench, "echo out; exit 7") == (7, "out\n")


def test_command_that_outlives_the_io_drain_timeout_keeps_its_output_and_exit_code(bench: Bench) -> None:
    # `sbx` cuts the output of an exec whose own process has exited half a minute earlier.
    assert run(bench, "echo start; sleep 45; echo done; exit 3") == (3, "start\ndone\n")


def test_agent_environment_holds_a_placeholder_token_and_no_github_token(bench: Bench) -> None:
    code, out = run(bench, 'echo "${CLAUDE_CODE_OAUTH_TOKEN:+set} ${GH_TOKEN:-none} $UV_PROJECT_ENVIRONMENT"')

    assert (code, out) == (0, "set none /home/agent/venv\n")


def test_kill_stops_the_command_and_its_children_in_the_vm(bench: Bench) -> None:
    log = bench.out / "kill.txt"
    process = bench.sandbox.exec(bench.name, ["sh", "-c", "sleep 901 & sleep 902; wait"], stdout=log)
    time.sleep(3)

    process.kill()

    time.sleep(2)
    code, out = run(bench, "ps -eo cmd | grep -c '^sleep 90[12]' || true")
    assert out.strip() == "0"


def test_command_of_a_stopped_sandbox_is_lost_with_its_status(bench: Bench) -> None:
    log = bench.out / "lost.txt"
    process = bench.sandbox.exec(bench.name, ["sleep", "60"], stdout=log)
    time.sleep(3)

    subprocess.run(["sbx", "stop", bench.name], check=True, capture_output=True)

    deadline = time.monotonic() + 60
    with pytest.raises(SandboxLost, match=f"Sandbox {bench.name} is stopped"):
        while process.poll() is None:
            assert time.monotonic() < deadline
            time.sleep(0.1)


def test_exec_starts_a_stopped_sandbox_without_writing_to_the_command_stderr(bench: Bench) -> None:
    subprocess.run(["sbx", "stop", bench.name], check=True, capture_output=True)
    err = bench.out / "start.err"

    process = bench.sandbox.exec(bench.name, ["true"], stdout=bench.out / "start.txt", stderr=err)

    deadline = time.monotonic() + 60
    while process.poll() is None:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    assert err.read_text() == ""


def test_agent_loads_only_the_factory_set_and_answers_on_the_subscription(bench: Bench) -> None:
    attempt_dir = bench.out / "attempt"
    attempt_dir.mkdir()
    (bench.out / "prompt.md").write_text("Answer with the word ok.")
    schema = {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}
    (bench.out / "schema.json").write_text(json.dumps(schema))
    (bench.out / "brief.json").write_text("{}")
    request = AttemptRequest(
        stage="contract",
        model="haiku",
        profile="hermetic",
        prompt=bench.out / "prompt.md",
        brief=bench.out / "brief.json",
        output_schema=bench.out / "schema.json",
        attempt_dir=attempt_dir,
        stream=attempt_dir / "stream.ndjson",
        sandbox=bench.name,
        workspace=bench.workspace,
        agent_home=bench.out / "agent",
    )

    handle = ClaudeCodeExecutor().start(request, bench.sandbox)

    deadline = time.monotonic() + 300
    while (result := handle.poll()) is None:
        assert time.monotonic() < deadline, "the agent gave no result in 300s"
        time.sleep(0.1)
    assert result.failure is None, result.reason
    assert isinstance(result.output, dict) and isinstance(result.output["answer"], str)
    events = [json.loads(line) for line in request.stream.read_text().splitlines() if line.strip()]
    init = next(e for e in events if e.get("subtype") == "init")
    kit = Kit()
    assert AgentSet.of_init(init) == kit.required("hermetic", bench.workspace) | kit.builtin
    assert (attempt_dir / "stderr.log").read_text() == ""


def test_describer_sandbox_under_its_own_name_reads_the_run_artifacts_and_writes_only_its_directory(
    bench: Bench,
) -> None:
    name, _, _ = describer_names(bench.out, "20261020-090000-abcd")
    reads, writes = bench.out / "spec-copy", bench.out / "describer"
    reads.mkdir()
    writes.mkdir()
    (reads / "spec.md").write_text("spec\n")
    bench.sandbox.create(
        name, bench.workspace, [Mount(bench.repo / ".git"), Mount(reads), Mount(writes, writable=True)]
    )
    try:
        code, out = run_in(bench, name, f"cat {reads}/spec.md && touch {writes}/note && ! touch {reads}/note")
        assert (code, out) == (0, "spec\n")
        assert (writes / "note").is_file()
    finally:
        assert bench.sandbox.remove(name) is None
