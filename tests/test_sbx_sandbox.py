import shlex
import sys
import time
from pathlib import Path

import pytest

from factory.claude_code import KIT
from factory.sandbox import SandboxSettings
from factory.sbx import TEMPLATE, SbxSandbox
from harness import AgentScript, Harness, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "wrote hello.txt"})
# Runs a command in a session of its own, so a crash that kills the agent's process group spares the command.
DETACHED = "import subprocess, sys; subprocess.Popen(sys.argv[1:], start_new_session=True).wait()"


def crash(factory: Harness) -> str:
    """Returns a shell command that stops every Sandbox the way a dying microVM does."""
    return shlex.join([sys.executable, "-c", DETACHED, *factory.sbx.command, "crash"])


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_ticket_gets_a_sandbox_from_the_pinned_template_with_explicit_resources(factory: Harness) -> None:
    factory.project["sandbox"] = {"kind": "sbx", "cpus": 2, "memory": "4g"}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [create] = factory.sbx.calls("create")
    assert flag(create, "--name") == f"{run_id}-t1"
    assert flag(create, "-t") == TEMPLATE
    assert flag(create, "--pull") == "missing"
    assert (flag(create, "--cpus"), flag(create, "--memory")) == ("2", "4g")
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"


def test_sandbox_mounts_every_path_at_its_host_path_with_only_the_ticket_outputs_writable(factory: Harness) -> None:
    factory.project["sandbox"] = {"kind": "sbx"}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO

    run_id = run_id_of(factory.run(spec).out)

    [create] = factory.sbx.calls("create")
    run_dir = factory.run_dir(run_id)
    git_dir = Path(factory.git("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    assert create[create.index("claude") + 1 :] == [
        str(factory.workspace(run_id, "t1")),
        f"{git_dir}:ro",
        str(run_dir / "tickets" / "t1"),
        f"{run_dir / 'spec.md'}:ro",
        f"{run_dir / 'tickets' / 't1' / 'ticket.md'}:ro",
        str(run_dir / "agent"),
        f"{KIT}:ro",
    ]


def sbx_project(factory: Harness, **settings: object) -> Path:
    factory.project["sandbox"] = {"kind": "sbx", **settings}
    factory.executor.scripts["implement"] = HELLO
    return factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})


def test_sandbox_egress_allows_only_the_claude_api_and_the_project_hosts(factory: Harness) -> None:
    spec = sbx_project(factory, egress=["pypi.org", "files.pythonhosted.org"])

    run_id = run_id_of(factory.run(spec).out)

    assert factory.sbx.calls("policy") == [
        ["check", "network", "--json", "egress-check.factory.invalid"],
        ["allow", "network", "--sandbox", f"{run_id}-t1", "api.anthropic.com,pypi.org,files.pythonhosted.org"],
    ]


def test_run_does_not_start_without_the_sbx_subscription_oauth(factory: Harness) -> None:
    spec = sbx_project(factory)
    factory.sbx.set(oauth=False)

    result = factory.run(spec)

    assert result.code == 2
    assert result.out == ""
    assert result.err.startswith("factory: sbx has no Claude subscription: run `sbx run claude`, sign in with /login")
    assert factory.sbx.calls("create") == []


def test_run_does_not_start_when_the_sbx_policy_allows_every_host(factory: Harness) -> None:
    spec = sbx_project(factory)
    factory.sbx.set(allow_all=True)

    result = factory.run(spec)

    assert result.code == 2
    assert result.err == "factory: sbx policy allows every host: run `sbx policy init deny-all`\n"


def test_agent_runs_with_a_placeholder_token_and_no_github_token_or_venv_in_the_workspace(
    factory: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "gho_owner")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-owner")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    spec = sbx_project(factory)

    run_id = run_id_of(factory.run(spec).out)

    [request] = factory.executor.requests
    launch = factory.executor.launch(request)
    assert "--dangerously-skip-permissions" in launch["argv"]
    assert "--permission-mode" not in launch["argv"]
    env = launch["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"]
    assert (env["GH_TOKEN"], env["ANTHROPIC_API_KEY"]) == (None, None)
    assert not Path(env["UV_PROJECT_ENVIRONMENT"]).is_relative_to(factory.workspace(run_id, "t1"))


def test_command_runs_under_the_stdin_watchdog_and_leaves_its_exit_code_in_the_attempt_dir(factory: Harness) -> None:
    spec = sbx_project(factory)

    assert factory.run(spec).code == 0

    [request] = factory.executor.requests
    assert (request.attempt_dir / "exit-code").read_text() == "0\n"
    [_, agent] = factory.sbx.calls("exec")
    assert agent[:2] == ["-i", "-w"]
    assert agent[agent.index(request.sandbox) + 1 : agent.index(request.sandbox) + 4] == ["setsid", "-w", "sh"]


def test_stopped_sandbox_is_started_before_exec_so_sbx_writes_nothing_to_stderr_log(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = "check"
    check = {"kind": "command", "with": {"run": "cat hello.txt"}, "next": {"pass": "merge", "fail": "abandoned"}}
    factory.pipeline["gates"] = {"check": check}
    spec = sbx_project(factory)
    factory.sbx.set(idle_stop=True)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    [request] = factory.executor.requests
    assert (request.attempt_dir / "stderr.log").read_text() == ""
    run_id = run_id_of(result.out)
    assert (factory.run_dir(run_id) / "tickets" / "t1" / "check" / "probe-1" / "log.txt").read_text() == "hello\n"
    warmups = [call for call in factory.sbx.calls("exec") if call[-1] == "true"]
    assert len(warmups) == 2


def test_killed_attempt_stops_the_agent_and_its_children_in_the_sandbox(factory: Harness) -> None:
    factory.pipeline["defaults"].update({"attempts": 2, "timeout": "5s"})
    spec = sbx_project(factory)
    factory.executor.scripts["implement"] = [AgentScript(files={"junk.txt": "half\n"}, fail="hang"), HELLO]

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [first, _] = factory.events(run_id, "AttemptFinished")
    assert first["cause"] == "timeout"
    assert not (factory.executor.requests[0].attempt_dir / "exit-code").exists()
    time.sleep(0.5)
    assert not (factory.workspace(run_id, "t1") / "late.txt").exists()


def test_attempt_whose_sandbox_dies_is_lost_with_the_sandbox_status(factory: Harness) -> None:
    spec = sbx_project(factory)
    factory.executor.scripts["implement"] = AgentScript(run=[f"{crash(factory)}; sleep 5"])

    result = factory.run(spec)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    finished = factory.events(run_id, "AttemptFinished")
    assert [e["cause"] for e in finished] == ["sandbox_lost"] * 3
    assert finished[0]["reason"].endswith(f"and no exit code, Sandbox {run_id}-t1 is stopped")
    assert factory.cli("status", run_id).out.splitlines()[2].split() == [
        "t1",
        "abandoned",
        "implement",
        "1",
        "3",
        "3",
        "abandoned",
    ]


def test_attempts_the_sandbox_lost_spend_no_attempt_and_the_next_one_gets_the_sandbox_again(
    factory: Harness,
) -> None:
    spec = sbx_project(factory)
    factory.executor.scripts["implement"] = [AgentScript(run=[f"{crash(factory)}; sleep 5"]), HELLO]

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    first, second = factory.events(run_id, "AttemptFinished")
    assert (first["cause"], second["outcome"]) == ("sandbox_lost", "done")
    assert [e["round"] for e in (first, second)] == [1, 1]
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"


def test_sandbox_that_sbx_cannot_delete_after_its_vm_died_stays_behind_without_blocking_the_run(
    factory: Harness,
) -> None:
    spec = sbx_project(factory)
    factory.sbx.set(rm_fails=True)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [left] = factory.events(run_id, "SandboxLeftBehind")
    assert left["sandbox"] == f"{run_id}-t1"
    assert "500 Internal Server Error" in left["reason"]
    assert factory.events(run_id, "SandboxRemoved") == []
    assert not factory.workspace(run_id, "t1").exists()
    assert f"factory/{run_id}--t1" not in factory.branches()


def test_stopped_sandbox_kills_what_ran_in_it_and_starts_again(factory: Harness, tmp_path: Path) -> None:
    sandbox = SbxSandbox(SandboxSettings(kind="sbx"), command=factory.sbx.command)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox.create("s1", workspace, [])
    process = sandbox.exec("s1", ["sh", "-c", "sleep 0.3; touch late.txt; sleep 30"], stdout=tmp_path / "out.txt")

    sandbox.stop("s1")

    assert process.poll() is not None
    assert factory.sbx.state()["sandboxes"]["s1"]["status"] == "stopped"
    time.sleep(0.5)
    assert not (workspace / "late.txt").exists()
    sandbox.start("s1")
    assert factory.sbx.state()["sandboxes"]["s1"]["status"] == "running"
    again = sandbox.exec("s1", ["true"], stdout=tmp_path / "out.txt")
    while (code := again.poll()) is None:
        time.sleep(0.01)
    assert code == 0


def crashing_gate(factory: Harness, crashes: int) -> None:
    """Puts the Gate `build` with two attempts after `implement`, whose first `crashes` Probes kill their Sandbox."""
    count = factory.root / "probes"
    run = f"n=$(cat {count} 2>/dev/null || echo 0); echo $((n + 1)) > {count}\n"
    run += f"if [ $n -lt {crashes} ]; then {crash(factory)}; sleep 5; fi; test -f hello.txt"
    factory.pipeline["defaults"]["sandbox_retries"] = 1
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {
        "build": {
            "kind": "command",
            "with": {"run": run},
            "attempts": 2,
            "next": {"pass": "merge", "fail": "abandoned"},
        }
    }


def test_probe_whose_sandbox_dies_is_lost_and_retried_without_a_verdict(factory: Harness) -> None:
    spec = sbx_project(factory)
    crashing_gate(factory, crashes=1)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")] == ["implement", "build", "merge", "done"]
    [lost] = factory.events(run_id, "ProbeLost")
    assert (lost["node"], lost["attempt"], lost["probe"]) == ("build", 1, 1)
    assert lost["reason"].endswith(f"and no exit code, Sandbox {run_id}-t1 is stopped")
    assert not (factory.run_dir(run_id) / "tickets" / "t1" / "build" / "probe-1" / "verdict.json").exists()
    [finished] = factory.events(run_id, "GateFinished")
    assert (finished["probe"], finished["outcome"]) == (2, "pass")


def test_gate_escalates_after_its_last_probe_whose_sandbox_died(factory: Harness) -> None:
    spec = sbx_project(factory)
    crashing_gate(factory, crashes=2)

    result = factory.run(spec)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")] == ["implement", "build", "abandoned"]
    assert [e["probe"] for e in factory.events(run_id, "ProbeLost")] == [1, 2]
    assert factory.events(run_id, "GateFinished") == []
