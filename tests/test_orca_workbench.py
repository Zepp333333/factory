import json
import re
import time
from pathlib import Path

import pytest

from factory import workbench as workbench_module
from factory.workbench import OrcaTerminal, OrcaWorkbench
from harness import AgentScript, Harness, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "wrote hello.txt"})
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def use_orca(factory: Harness) -> None:
    factory.workbench_override = OrcaWorkbench(command=factory.orca.command)


def test_workspaces_are_shown_in_orca_and_the_run_still_merges(factory: Harness) -> None:
    use_orca(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    [add] = factory.orca.calls("repo", "add")
    assert str(factory.repo) in add
    [visibility] = factory.orca.calls("repo", "set")
    assert visibility[visibility.index("--external-worktree-visibility") + 1] == "show"
    removed = factory.orca.calls("worktree", "rm")
    assert [call[call.index("--worktree") + 1] for call in removed] == [f"path:{factory.workspace(run_id, 't1')}"]
    assert not factory.workspace(run_id, "t1").exists()


def test_attempt_terminal_opens_in_orca_on_the_workspace_and_shows_the_renderer(factory: Harness) -> None:
    use_orca(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [create] = factory.orca.calls("terminal", "create")
    assert create[create.index("--worktree") + 1] == f"path:{factory.workspace(run_id, 't1')}"
    assert create[create.index("--title") + 1] == "implement"
    shown = ANSI.sub("", factory.orca.output("term-1")).splitlines()
    assert "Done." in shown
    assert any(line.startswith("result success") for line in shown)
    log = ANSI.sub("", (factory.executor.requests[0].attempt_dir / "terminal.log").read_text())
    assert "Done." in log.splitlines()


def test_workspace_card_shows_the_current_stage_and_round(factory: Harness) -> None:
    use_orca(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    comments = [c[c.index("--comment") + 1] for c in factory.orca.calls("worktree", "set")]
    assert comments[0] == "implement, Round 1"
    assert all(
        c[c.index("--worktree") + 1] == f"path:{factory.workspace(run_id, 't1')}"
        for c in factory.orca.calls("worktree", "set")
    )


def test_orca_that_fails_stops_the_run_with_its_error(factory: Harness) -> None:
    factory.workbench_override = OrcaWorkbench(command=("false",))
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO

    result = factory.run(spec)

    assert result.code != 0
    assert "orca" in result.err


def test_workbench_answers_reconciliation_for_workspaces_and_terminals(factory: Harness, tmp_path_factory) -> None:  # type: ignore[no-untyped-def]
    workbench = OrcaWorkbench(command=factory.orca.command)
    path = tmp_path_factory.mktemp("ws") / "w"
    factory.git("branch", "ticket")
    assert not workbench.has_workspace(factory.repo, path)

    workbench.create_workspace(factory.repo, path, "ticket")
    assert workbench.has_workspace(factory.repo, path)
    stream = path.parent / "stream.ndjson"
    stream.write_text("")
    terminal = workbench.open_attempt(stream, path.parent / "terminal.log", path, "implement")
    assert terminal.alive()
    terminal.close()
    assert not terminal.alive()

    workbench.remove_workspace(factory.repo, path)
    assert not workbench.has_workspace(factory.repo, path)


def test_terminal_is_not_alive_when_only_a_longer_handle_is(factory: Harness, tmp_path_factory) -> None:  # type: ignore[no-untyped-def]
    workbench = OrcaWorkbench(command=factory.orca.command)
    root = tmp_path_factory.mktemp("ws")
    factory.git("branch", "ticket")
    workbench.create_workspace(factory.repo, root / "w2", "ticket")
    stream = root / "stream.ndjson"
    stream.write_text("")
    terminal = workbench.open_attempt(stream, root / "terminal.log", root / "w2", "implement")

    assert not OrcaTerminal(workbench, root / "w2", "term", root / "terminal.log").alive()
    terminal.close()


def test_terminal_ends_when_the_renderer_shows_the_result(factory: Harness, tmp_path_factory) -> None:  # type: ignore[no-untyped-def]
    workbench = OrcaWorkbench(command=factory.orca.command)
    root = tmp_path_factory.mktemp("ws")
    factory.git("branch", "ticket")
    workbench.create_workspace(factory.repo, root / "w", "ticket")
    stream = root / "stream.ndjson"
    stream.write_text('{"type": "result", "subtype": "success", "num_turns": 1, "duration_ms": 1, "result": "ok"}\n')

    terminal = workbench.open_attempt(stream, root / "terminal.log", root / "w", "implement")

    deadline = time.monotonic() + 10
    while terminal.alive():
        assert time.monotonic() < deadline, "the terminal outlived its renderer"
        time.sleep(0.1)


def test_close_closes_a_terminal_that_does_not_exit(
    factory: Harness, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workbench_module, "RENDERER_GRACE_SECONDS", 0.2)
    workbench = OrcaWorkbench(command=factory.orca.command)
    root = tmp_path_factory.mktemp("ws")
    factory.git("branch", "ticket")
    workbench.create_workspace(factory.repo, root / "w", "ticket")
    created = workbench.call(
        "terminal", "create", "--worktree", f"path:{root / 'w'}", "--title", "stuck", "--command", "true", "--json"
    )
    handle = json.loads(created)["result"]["terminal"]["handle"]
    terminal = OrcaTerminal(workbench, root / "w", handle, root / "terminal.log")

    terminal.close()

    assert not terminal.alive()


def test_orca_is_given_the_main_checkout_when_the_repo_is_a_worktree(factory: Harness, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    factory.git("worktree", "add", "-q", "-b", "elsewhere", str(linked))
    factory.git("branch", "factory/ticket")
    workbench = OrcaWorkbench(command=factory.orca.command)

    workbench.create_workspace(linked, tmp_path / "ws", "factory/ticket")

    [add] = factory.orca.calls("repo", "add")
    assert add[add.index("--path") + 1] == str(factory.repo.resolve())
    [visibility] = factory.orca.calls("repo", "set")
    assert visibility[visibility.index("--repo") + 1] == f"path:{factory.repo.resolve()}"
