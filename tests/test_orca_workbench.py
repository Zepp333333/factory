import json
import re
from pathlib import Path

import pytest

from factory.adapters import terminal_workbench as workbench_module
from factory.adapters.terminal_workbench import OrcaTerminal, OrcaWorkbench
from harness import AgentScript, Harness, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "wrote hello.txt"})
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def use_orca(factory: Harness) -> None:
    factory.workbench_override = OrcaWorkbench(command=factory.orca.command)


def test_workspace_and_attempt_terminal_are_shown_in_orca_with_the_stage_card_and_the_run_still_merges(
    factory: Harness,
) -> None:
    use_orca(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    worktree = f"path:{factory.workspace(run_id, 't1')}"
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    [add] = factory.orca.calls("repo", "add")
    assert str(factory.repo) in add
    [visibility] = factory.orca.calls("repo", "set")
    assert visibility[visibility.index("--external-worktree-visibility") + 1] == "show"

    [create] = factory.orca.calls("terminal", "create")
    assert create[create.index("--worktree") + 1] == worktree
    assert create[create.index("--title") + 1] == "implement"
    shown = ANSI.sub("", factory.orca.output("term-1")).splitlines()
    assert "Done." in shown
    assert any(line.startswith("result success") for line in shown)
    log = ANSI.sub("", (factory.executor.requests[0].attempt_dir / "terminal.log").read_text())
    assert "Done." in log.splitlines()

    cards = factory.orca.calls("worktree", "set")
    assert cards[0][cards[0].index("--comment") + 1] == "implement, Round 1"
    assert all(card[card.index("--worktree") + 1] == worktree for card in cards)

    removed = factory.orca.calls("worktree", "rm")
    assert [call[call.index("--worktree") + 1] for call in removed] == [worktree]
    assert not factory.workspace(run_id, "t1").exists()


def test_orca_that_fails_stops_the_run_with_its_error(factory: Harness) -> None:
    factory.workbench_override = OrcaWorkbench(command=("false",))
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = HELLO

    result = factory.run(spec)

    assert result.code != 0
    assert "orca" in result.err


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
    assert not OrcaTerminal(workbench, root / "w", "term", root / "terminal.log").alive()

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
