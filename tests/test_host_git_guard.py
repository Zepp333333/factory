from pathlib import Path

import pytest

from factory.git import SAFE_CONFIG
from harness import AgentScript, Harness, git, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
HOOKS = ["pre-commit", "commit-msg", "post-commit", "reference-transaction", "post-checkout", "post-index-change"]


def tripwire(factory: Harness, name: str) -> str:
    """Returns a shell script that leaves a mark under `ran/` when anything executes it."""
    ran = factory.root / "ran"
    ran.mkdir(exist_ok=True)
    return f"#!/bin/sh\ntouch {ran / name}\n"


def marks(factory: Harness) -> list[str]:
    ran = factory.root / "ran"
    return sorted(path.name for path in ran.iterdir()) if ran.exists() else []


def all_refs(factory: Harness) -> dict[str, str]:
    lines = factory.git("for-each-ref", "--format=%(refname) %(objectname)").splitlines()
    return dict(line.split(" ", 1) for line in lines)


def hostile_repo(factory: Harness) -> Path:
    """Creates a repository outside the Target project whose hooks and fsmonitor leave marks."""
    evil = factory.root / "evil"
    evil.mkdir()
    git(evil, "init", "-q", "-b", "main")
    git(evil, "config", "user.name", "Evil")
    git(evil, "config", "user.email", "evil@example.com")
    git(evil, "commit", "-q", "--allow-empty", "-m", "evil")
    script = evil / "fsmonitor.sh"
    script.write_text(tripwire(factory, "evil-fsmonitor"))
    script.chmod(0o755)
    git(evil, "config", "core.fsmonitor", str(script))
    for hook in HOOKS:
        path = evil / ".git" / "hooks" / hook
        path.write_text(tripwire(factory, f"evil-{hook}"))
        path.chmod(0o755)
    return evil


def keep_workspace(factory: Harness) -> None:
    """Makes the Sandbox refuse removal, so the cleanup keeps the Workspace for the test to inspect."""
    factory.sandbox.refuse_remove = True


def test_agent_pointing_the_worktree_git_file_elsewhere_does_not_redirect_host_git(factory: Harness) -> None:
    evil = hostile_repo(factory)
    evil_head = git(evil, "rev-parse", "HEAD")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n", ".git": f"gitdir: {evil / '.git'}\n"},
        output={"summary": "Add hello.txt", "handoff": "done"},
    )

    keep_workspace(factory)

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    assert marks(factory) == []
    assert git(evil, "rev-parse", "HEAD") == evil_head
    assert git(evil, *SAFE_CONFIG, "status", "--porcelain", "--untracked-files=no") == ""
    workspace = factory.workspace(run_id, "t1")
    gitdir = (factory.repo / ".git" / "worktrees" / "t1").resolve()
    assert (workspace / ".git").read_text() == f"gitdir: {gitdir}\n"
    assert git(workspace, "status", "--porcelain") == ""


def test_worktree_git_file_replaced_by_a_directory_is_restored(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        remove=[".git"],
        files={
            "hello.txt": "hello\n",
            ".git/config": "[core]\n\tfsmonitor = .git/hooks/fsmonitor\n",
            ".git/hooks/fsmonitor": tripwire(factory, "fsmonitor"),
            ".git/hooks/pre-commit": tripwire(factory, "pre-commit"),
        },
        run=["chmod +x .git/hooks/fsmonitor .git/hooks/pre-commit"],
        output={"summary": "Add hello.txt", "handoff": "done"},
    )

    keep_workspace(factory)

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert factory.git("ls-tree", "--name-only", f"factory/{run_id}").splitlines() == [
        "README.md",
        "factory.yaml",
        "hello.txt",
    ]
    assert marks(factory) == []
    workspace = factory.workspace(run_id, "t1")
    assert (workspace / ".git").is_file()
    assert git(workspace, "status", "--porcelain") == ""


@pytest.mark.parametrize("fails_first", [False, True], ids=["commit", "discard"])
def test_hooks_planted_by_the_agent_never_run(factory: Harness, fails_first: bool) -> None:
    factory.git("config", "core.hooksPath", ".githooks")
    factory.pipeline["defaults"]["attempts"] = 2
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    planted = {f".githooks/{hook}": tripwire(factory, hook) for hook in HOOKS}
    done = AgentScript(
        files={"hello.txt": "hello\n", **planted},
        run=["chmod +x .githooks/*"],
        output={"summary": "Add hello.txt", "handoff": "done"},
    )
    crash = AgentScript(files=planted, run=["chmod +x .githooks/*"], fail="crash")
    factory.executor.scripts["implement"] = [crash, done] if fails_first else done

    result = factory.run(spec)

    assert result.code == 0, result.out
    assert marks(factory) == []


@pytest.mark.parametrize("fails_first", [False, True], ids=["commit", "discard"])
def test_config_of_a_repository_nested_in_the_workspace_never_runs(factory: Harness, fails_first: bool) -> None:
    """The agent switches the planted filter off in its own git calls, so only a host call can leave a mark."""
    filter = factory.root / "filter.sh"
    filter.write_text(tripwire(factory, "filter"))
    filter.chmod(0o755)
    factory.pipeline["defaults"]["attempts"] = 2
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    nest = [
        "git init -q nested",
        "git -C nested -c user.name=Agent -c user.email=agent@example.com commit -q --allow-empty -m nested",
        f"git -C nested config filter.planted.clean {filter}",
        f"git -C nested config core.fsmonitor {filter}",
        "echo '* filter=planted' > nested/.gitattributes && echo x > nested/a.txt",
        "git -C nested -c core.fsmonitor=false -c filter.planted.clean=cat add -A",
        "echo y > nested/a.txt",
    ]
    output = {"summary": "Add hello.txt", "handoff": "done"}
    if fails_first:
        scripts = [AgentScript(run=nest, fail="crash"), AgentScript(files={"hello.txt": "hello\n"}, output=output)]
    else:
        scripts = [AgentScript(files={"hello.txt": "hello\n"}, run=nest, output=output)]
    factory.executor.scripts["implement"] = scripts

    result = factory.run(spec)

    assert result.code == 0, result.out
    assert marks(factory) == []
    nested = factory.workspace(run_id_of(result.out), "t1") / "nested"
    assert nested.exists() != fails_first


def test_fsmonitor_from_the_workspace_config_never_runs(factory: Harness) -> None:
    script = factory.root / "fsmonitor.sh"
    script.write_text(tripwire(factory, "fsmonitor"))
    script.chmod(0o755)
    factory.git("config", "core.fsmonitor", str(script))
    factory.pipeline["defaults"]["attempts"] = 2
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [
        AgentScript(files={"junk.txt": "half\n"}, fail="crash"),
        AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"}),
    ]

    result = factory.run(spec)

    assert result.code == 0, result.out
    assert marks(factory) == []


def test_engine_writes_no_ref_outside_its_namespaces_even_when_the_agent_moves_head(factory: Harness) -> None:
    factory.git("tag", "v1")
    before = all_refs(factory)
    worktree_head = factory.repo / ".git" / "worktrees" / "t1" / "HEAD"
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n", str(worktree_head): "ref: refs/heads/feature\n"},
        output={"summary": "Add hello.txt", "handoff": "done"},
    )

    keep_workspace(factory)

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    after = all_refs(factory)
    assert {ref: after.get(ref) for ref in before} == before
    assert all(ref.startswith(f"refs/heads/factory/{run_id}") for ref in after.keys() - before.keys())
    assert git(factory.workspace(run_id, "t1"), "symbolic-ref", "HEAD") == f"refs/heads/factory/{run_id}--t1"


def test_workspace_redirected_right_before_cleanup_is_kept_and_the_other_repository_untouched(
    factory: Harness,
) -> None:
    evil = hostile_repo(factory)
    evil_head = git(evil, "rev-parse", "HEAD")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"}
    )
    factory.workbench.late_files = {".git": f"gitdir: {evil / '.git'}\n"}

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert marks(factory) == []
    assert git(evil, "rev-parse", "HEAD") == evil_head
    assert (evil / ".git").is_dir()
    assert [e["step"] for e in factory.events(run_id, "CleanupFailed")] == ["workspace"]
    assert f"factory/{run_id}--t1" in factory.branches()
