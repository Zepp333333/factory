import os
import shlex
import sys
from pathlib import Path

import pytest

from factory.adapters.sbx import TEMPLATE
from harness import AgentScript, Harness, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "wrote hello.txt"})
# Runs a command in a session of its own, so a crash that kills the agent's process group spares the command.
DETACHED = "import subprocess, sys; subprocess.Popen(sys.argv[1:], start_new_session=True).wait()"
# Starts a command in a session of its own and leaves it running, so only a Sandbox that kills by environment finds it.
LEFT_BEHIND = "import subprocess, sys; subprocess.Popen(sys.argv[1:], start_new_session=True)"


def crash(factory: Harness) -> str:
    """Returns a shell command that stops every Sandbox the way a dying microVM does."""
    return shlex.join([sys.executable, "-c", DETACHED, *factory.sbx.command, "crash"])


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def sbx_project(factory: Harness, **settings: object) -> Path:
    factory.project["sandbox"] = {"kind": "sbx", **settings}
    factory.executor.scripts["implement"] = HELLO
    return factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})


def gate(factory: Harness, run: str) -> None:
    """Puts the Gate `check` that runs `run` after `implement`."""
    factory.pipeline["stages"]["implement"]["next"]["done"] = "check"
    factory.pipeline["gates"] = {
        "check": {"kind": "command", "with": {"run": run}, "next": {"pass": "merge", "fail": "abandoned"}}
    }


def test_ticket_runs_in_a_sbx_sandbox_built_from_the_project_settings_and_left_behind_when_sbx_cannot_delete_it(
    factory: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One Run checks the Sandbox's template, resources, mounts, egress, environment, watchdog, reap and removal."""
    monkeypatch.setenv("GH_TOKEN", "gho_owner")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-owner")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    settings = {"cpus": 2, "memory": "4g", "egress": ["pypi.org", "files.pythonhosted.org"]}
    env = {"UV_PROJECT_ENVIRONMENT": "/home/agent/venv", "TOOLCHAIN": "swift-6"}
    spec = sbx_project(factory, **settings, env=env)
    gate(factory, 'printf %s "$TOOLCHAIN"; cat hello.txt')
    pidfile = factory.root / "background.pid"
    sleep = shlex.join([sys.executable, "-c", LEFT_BEHIND, "sh", "-c", f"echo $$ > {pidfile}; exec sleep 300"])
    factory.executor.scripts["implement"] = AgentScript(files=HELLO.files, output=HELLO.output, run=[sleep])
    factory.sbx.set(idle_stop=True, rm_fails=True, notice_after_json="update available: 0.50.0 (running 0.49.1)")

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    run_dir = factory.run_dir(run_id)
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"

    [create] = factory.sbx.calls("create")
    assert flag(create, "--name") == f"{run_id}-t1"
    assert flag(create, "-t") == TEMPLATE
    assert flag(create, "--pull") == "missing"
    assert (flag(create, "--cpus"), flag(create, "--memory")) == ("2", "4g")
    git_dir = Path(factory.git("rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    assert create[create.index("claude") + 1 :] == [
        str(factory.workspace(run_id, "t1")),
        f"{git_dir}:ro",
        str(run_dir / "tickets" / "t1"),
        f"{run_dir / 'spec.md'}:ro",
        f"{run_dir / 'tickets' / 't1' / 'ticket.md'}:ro",
        str(run_dir / "agent"),
        f"{run_dir / 'kit'}:ro",
    ]
    assert factory.sbx.calls("policy") == [
        ["check", "network", "--json", "egress-check.factory.invalid"],
        ["allow", "network", "--sandbox", f"{run_id}-t1", "api.anthropic.com,pypi.org,files.pythonhosted.org"],
    ]

    [request] = factory.executor.requests
    launch = factory.executor.launch(request)
    assert "--dangerously-skip-permissions" in launch["argv"]
    assert "--permission-mode" not in launch["argv"]
    assert launch["env"]["CLAUDE_CODE_OAUTH_TOKEN"]
    assert (launch["env"]["GH_TOKEN"], launch["env"]["ANTHROPIC_API_KEY"]) == (None, None)
    assert launch["env"]["UV_PROJECT_ENVIRONMENT"] == "/home/agent/venv"
    assert launch["env"]["TOOLCHAIN"] == "swift-6"
    assert (run_dir / "tickets" / "t1" / "check" / "probe-1" / "log.txt").read_text() == "swift-6hello\n"
    [started] = factory.events(run_id, "RunStarted")
    assert started["sandbox_egress"] == ["api.anthropic.com", "pypi.org", "files.pythonhosted.org"]
    assert started["sandbox_env"] == ["TOOLCHAIN", "UV_PROJECT_ENVIRONMENT"]
    assert "swift-6" not in str(started)

    # The agent runs under the stdin watchdog, and each command finds its stopped Sandbox started first.
    assert (request.attempt_dir / "exit-code").read_text() == "0\n"
    assert (request.attempt_dir / "stderr.log").read_text() == ""
    agent, _probe = [call for call in factory.sbx.calls("exec") if "setsid" in call]
    assert agent[:2] == ["-i", "-w"]
    assert agent[agent.index(request.sandbox) + 1 : agent.index(request.sandbox) + 4] == ["setsid", "-w", "sh"]
    assert len([call for call in factory.sbx.calls("exec") if call[-1] == "true"]) == 2

    # The Probe's start kills what the agent left running in the Sandbox.
    [killed] = factory.events(run_id, "SandboxProcessKilled")
    assert killed["command"].strip().endswith("sleep 300")
    assert killed["pid"] == int(pidfile.read_text())
    assert not alive(killed["pid"])

    [left] = factory.events(run_id, "SandboxLeftBehind")
    assert left["sandbox"] == f"{run_id}-t1"
    assert "500 Internal Server Error" in left["reason"]
    assert factory.events(run_id, "SandboxRemoved") == []
    assert not factory.workspace(run_id, "t1").exists()
    assert f"factory/{run_id}--t1" not in factory.branches()


def test_run_does_not_start_without_the_sbx_subscription_oauth(factory: Harness) -> None:
    spec = sbx_project(factory)
    factory.sbx.set(oauth=False)

    result = factory.run(spec)

    assert result.code == 2
    assert result.out == ""
    assert result.err.startswith("factory: sbx has no Claude subscription: run `sbx run claude`, sign in with /login")
    assert factory.sbx.calls("create") == []


def test_sbx_output_that_is_not_json_is_reported_with_its_position_and_the_text_around_it(factory: Harness) -> None:
    spec = sbx_project(factory)
    factory.sbx.set(notice_before_json="THE-NOTICE update available\n", notice_after_json="THE-TAIL " + "x" * 200)

    result = factory.run(spec)

    assert result.code == 2
    assert "sbx gave no JSON at char 0 " in result.err
    assert "THE-NOTICE" in result.err
    assert "ending with '" + "x" * 99 in result.err
    assert result.err.count("THE-NOTICE") == 1


def test_run_does_not_start_when_the_sbx_policy_allows_every_host(factory: Harness) -> None:
    spec = sbx_project(factory)
    factory.sbx.set(allow_all=True)

    result = factory.run(spec)

    assert result.code == 2
    assert result.err == "factory: sbx policy allows every host: run `sbx policy init deny-all`\n"


def test_attempt_and_probe_whose_sandbox_dies_are_lost_with_the_sandbox_status_and_retried_in_the_same_round(
    factory: Harness,
) -> None:
    spec = sbx_project(factory)
    factory.executor.scripts["implement"] = [AgentScript(run=[f"{crash(factory)}; sleep 5"]), HELLO]
    count = factory.root / "probes"
    gate(factory, f"if [ ! -e {count} ]; then touch {count}; {crash(factory)}; sleep 5; fi; test -f hello.txt")

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    stopped = f"and no exit code, Sandbox {run_id}-t1 is stopped"
    first, second = factory.events(run_id, "AttemptFinished")
    assert (first["cause"], second["outcome"]) == ("sandbox_lost", "done")
    assert first["reason"].endswith(stopped)
    assert [e["round"] for e in (first, second)] == [1, 1]
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")] == ["implement", "check", "merge", "done"]
    [lost] = factory.events(run_id, "ProbeLost")
    assert (lost["node"], lost["attempt"], lost["probe"]) == ("check", 1, 1)
    assert lost["reason"].endswith(stopped)
    assert not (factory.run_dir(run_id) / "tickets" / "t1" / "check" / "probe-1" / "verdict.json").exists()
    [finished] = factory.events(run_id, "GateFinished")
    assert (finished["probe"], finished["outcome"]) == (2, "pass")
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
