import shutil

from harness import AgentScript, Harness, run_id_of
from test_gitlab_run import PROJECT, publish

APPEND = AgentScript(run=["echo line >> log.txt"], output={"summary": "Append a line", "handoff": "appended"})
TITLE = "'Greeting: Greet, Greet again'"
MR_REPO = f"gitlab.example.com/{PROJECT}"


def landing(factory: Harness) -> str:
    """Publishes the Spec and leaves `origin` as the only GitLab remote of its project, pushing to the bare repo."""
    ssh = f"git@gitlab.example.com:{PROJECT}.git"
    factory.git("remote", "remove", "gitlab")
    factory.git("remote", "set-url", "origin", ssh)
    factory.git("config", f"url.{factory.remote}.insteadOf", ssh)
    return publish(factory)


def test_run_that_merged_tickets_pushes_the_integration_branch_and_opens_an_mr_closing_them(factory: Harness) -> None:
    url = landing(factory)
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    integration = f"factory/{run_id}"
    assert factory.remote_branches() == {integration: factory.git("rev-parse", integration)}
    [merge_request] = factory.glab.merge_requests()
    assert merge_request == {
        "repo": MR_REPO,
        "base": "main",
        "head": integration,
        "title": "Greeting: Greet, Greet again",
        "body": f"Lands Spec #21 from Run {run_id}.\n\nCloses #28\nCloses #30\n",
    }
    [opened] = factory.events(run_id, "PullRequestOpened")
    assert opened["url"] == f"https://{MR_REPO}/-/merge_requests/1"
    assert result.out.splitlines()[-2:] == [
        f"Run {run_id} finished: done",
        f"Pull request: https://{MR_REPO}/-/merge_requests/1",
    ]


def test_mr_lists_the_abandoned_tickets_and_those_they_left_waiting(factory: Harness) -> None:
    landing(factory)
    factory.glab.issue(28, "Greet", "Write hello.txt.")
    factory.glab.issue(30, "Greet again", "Write bye.txt.", blocked_by=[28])
    factory.glab.set(linked_items=True)
    factory.glab.issue(31, "Wave", "Write wave.txt.")
    url = factory.glab.issue(21, "Greeting", "Say hello.", kind="Issue", children=[28, 30, 31])
    factory.executor.scripts["implement"] = APPEND
    factory.pipeline["defaults"]["sandbox_retries"] = 0
    factory.sandbox.refuse = 1

    result = factory.run(url)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    [merge_request] = factory.glab.merge_requests()
    assert merge_request["body"] == (
        f"Lands Spec #21 from Run {run_id}.\n\n"
        "Closes #31\n\n"
        "Not landed:\n\n"
        "- #28 Greet: abandoned, blocking #30\n"
        "- #30 Greet again: not started, blocked by #28\n"
    )


def test_run_that_merged_no_ticket_opens_no_mr(factory: Harness) -> None:
    url = landing(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.pipeline["defaults"]["sandbox_retries"] = 0
    factory.sandbox.refuse = 1

    result = factory.run(url)

    assert result.code == 1, result.out + result.err
    assert factory.glab.merge_requests() == []
    assert factory.remote_branches() == {}


def test_failed_push_is_journaled_and_prints_the_commands_to_land_it_by_hand(factory: Harness) -> None:
    url = landing(factory)
    factory.executor.scripts["implement"] = APPEND
    shutil.rmtree(factory.remote)

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [failed] = factory.events(run_id, "LandingFailed")
    assert failed["step"] == "push"
    assert factory.glab.merge_requests() == []
    by_hand = result.out.partition("Land it by hand:\n")[2]
    push, mr = by_hand.split("\n", 1)
    assert push == f"  git push origin factory/{run_id}"
    assert mr.startswith(f"  glab mr create --repo {MR_REPO} --source-branch factory/{run_id} --target-branch main ")


def test_failed_mr_is_journaled_and_prints_the_command_to_open_it(factory: Harness) -> None:
    url = landing(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.glab.set(mr_fails=True)

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert f"factory/{run_id}" in factory.remote_branches()
    [failed] = factory.events(run_id, "LandingFailed")
    assert failed["step"] == "pull_request"
    assert failed["reason"].startswith("glab mr failed: ")
    by_hand = result.out.partition("Land it by hand:\n")[2]
    assert by_hand.startswith(f"  glab mr create --repo {MR_REPO} --source-branch factory/{run_id} ")
    assert f" --title {TITLE} " in by_hand
    assert "Pull request:" not in result.out
    assert factory.cli("status", run_id).out.splitlines()[0] == f"Run {run_id}  done  factory/{run_id}"


def test_push_goes_to_the_remote_of_the_spec_project(factory: Harness) -> None:
    url = landing(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.git("remote", "rename", "origin", "upstream")
    factory.git("remote", "add", "origin", "git@gitlab.example.com:someone/fork.git")

    result = factory.run(url)

    [pushed] = factory.events(run_id_of(result.out), "IntegrationBranchPushed")
    assert pushed["remote"] == "upstream"


def test_run_warns_when_base_is_not_the_default_branch_of_the_project(factory: Harness) -> None:
    url = landing(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.glab.set(default_branch="develop")

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    assert result.err == (
        f"factory: warning: base main is not the default branch develop of {PROJECT},"
        " so GitLab will not close the merged Tickets and they stay open\n"
    )


def test_run_on_the_default_branch_does_not_warn(factory: Harness) -> None:
    url = landing(factory)
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url)

    assert result.err == ""
