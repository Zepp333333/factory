import shutil

from harness import AgentScript, Harness, git, run_id_of
from test_github_run import publish

APPEND = AgentScript(run=["echo line >> log.txt"], output={"summary": "Append a line", "handoff": "appended"})


def advance_remote(factory: Harness) -> str:
    """Lands a commit on the remote's `main` from another clone and returns it."""
    other = factory.root / "other"
    git(factory.root, "clone", "-q", "-b", "main", str(factory.remote), str(other))
    git(other, "config", "user.name", "Colleague")
    git(other, "config", "user.email", "colleague@example.com")
    (other / "upstream.txt").write_text("merged elsewhere\n")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "Merged elsewhere")
    git(other, "push", "-q", "origin", "main")
    return git(other, "rev-parse", "HEAD")


def test_run_whose_local_base_lags_behind_the_remote_starts_from_the_remote_commit(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND
    local = factory.git("rev-parse", "main")
    status = factory.git("status", "--porcelain")
    remote = advance_remote(factory)

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [created] = factory.events(run_id, "IntegrationBranchCreated")
    assert created["commit"] == remote
    assert factory.git("merge-base", "--is-ancestor", remote, f"factory/{run_id}") == ""
    assert factory.git("rev-parse", "main") == local
    assert factory.git("status", "--porcelain") == status
    assert not (factory.repo / "upstream.txt").exists()


def test_local_commits_on_base_that_the_remote_lacks_stop_the_run_and_are_named(factory: Harness) -> None:
    url = publish(factory)
    (factory.repo / "local.txt").write_text("not pushed\n")
    factory.git("add", "-A")
    factory.git("commit", "-q", "-m", "Work only here")
    before = factory.branches()

    result = factory.run(url)

    assert result.code == 2
    assert "started" not in result.out
    assert "Work only here" in result.err
    assert factory.branches() == before


def test_failed_fetch_stops_the_run_and_names_the_command(factory: Harness) -> None:
    url = publish(factory)
    shutil.rmtree(factory.remote)
    before = factory.branches()

    result = factory.run(url)

    assert result.code == 2
    assert "started" not in result.out
    assert "git fetch --quiet origin refs/heads/main" in result.err
    assert factory.branches() == before


def test_project_without_a_remote_runs_from_the_local_base_and_says_so_once(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": "Write hello.txt.\n"})
    factory.executor.scripts["implement"] = APPEND
    for name in factory.git("remote").splitlines():
        factory.git("remote", "remove", name)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    [created] = factory.events(run_id_of(result.out), "IntegrationBranchCreated")
    assert created["commit"] == factory.git("rev-parse", "main")
    assert result.err.count("starts from the local main") == 1
