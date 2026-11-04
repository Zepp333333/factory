import sqlite3

from harness import READY, REPO, AgentScript, CliResult, Harness, run_id_of

TITLE = "'Greeting: Greet, Greet again'"

APPEND = AgentScript(run=["echo line >> log.txt"], output={"summary": "Append a line", "handoff": "appended"})


def publish(factory: Harness) -> str:
    """Publishes Spec #21 with sub-issues #28 and #30, where #28 waits on the closed #27 and #30 on #28."""
    factory.gh.issue(27, "Lay the ground", state="closed")
    factory.gh.issue(28, "Greet", "Write hello.txt.", blocked_by=[27])
    factory.gh.issue(30, "Greet again", "Write bye.txt.", blocked_by=[28])
    return factory.gh.issue(21, "Greeting", "Say hello.", sub_issues=[28, 30])


def runs(factory: Harness) -> list[str]:
    db = factory.home / "factory.db"
    if not db.exists():
        return []
    with sqlite3.connect(db) as connection:
        return [run_id for (run_id,) in connection.execute("SELECT id FROM runs")]


def assert_refused(factory: Harness, result: CliResult, before: dict[str, str]) -> None:
    assert result.code == 2
    assert "started" not in result.out
    assert factory.branches() == before
    assert runs(factory) == []


def test_run_that_merged_no_ticket_opens_no_pr(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.pipeline["defaults"]["sandbox_retries"] = 0
    factory.sandbox.refuse = 1

    result = factory.run(url)

    run_id = run_id_of(result.out)
    assert factory.remote_branches() == {}
    assert factory.gh.pulls() == []
    assert factory.events(run_id, "LandingFailed") == []


def test_tickets_flag_takes_the_listed_sub_issues_once_and_a_closed_blocker_in_another_repository_counts_as_done(
    factory: Harness,
) -> None:
    factory.gh.issue(27, "Lay the ground", state="closed")
    factory.gh.issue(5, "Elsewhere", state="closed", repo="owner/other")
    factory.gh.issue(28, "Greet", "Write hello.txt.", blocked_by=[27, "owner/other#5"])
    factory.gh.issue(30, "Greet again", "Write bye.txt.", blocked_by=[28])
    url = factory.gh.issue(21, "Greeting", "Say hello.", sub_issues=[28, 30])
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url, "--tickets", "28,28")

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [snapshotted] = factory.events(run_id, "SpecSnapshotted")
    assert snapshotted["snapshot"]["tickets"] == [
        {
            "id": "28",
            "title": "Greet",
            "body": "Write hello.txt.",
            "blocked_by": [],
            "labels": [READY],
            "blocked_outside": {},
        }
    ]
    assert factory.gh.pulls()[0]["body"] == f"Lands Spec #21 from Run {run_id}.\n\nCloses #28\n"


def test_ticket_that_is_not_a_sub_issue_of_the_spec_stops_the_run(factory: Harness) -> None:
    url = publish(factory)
    before = factory.branches()

    result = factory.run(url, "--tickets", "28,27")

    assert_refused(factory, result, before)
    assert result.err == f"factory: #27 is not a sub-issue of {url}\n"


def test_tickets_flag_refuses_anything_but_issue_numbers(factory: Harness) -> None:
    url = publish(factory)

    result = factory.run(url, "--tickets", "28,#30")

    assert result.code == 2
    assert "--tickets" in result.err


def test_open_blocker_in_another_repository_stops_the_run(factory: Harness) -> None:
    factory.gh.issue(5, "Elsewhere", repo="owner/other")
    factory.gh.issue(28, "Greet", blocked_by=["owner/other#5"])
    url = factory.gh.issue(21, "Greeting", sub_issues=[28])
    before = factory.branches()

    result = factory.run(url)

    assert_refused(factory, result, before)
    assert result.err == "factory: #28 is blocked by open owner/other#5, which this Run does not take\n"


def test_sub_issue_in_another_repository_stops_the_run(factory: Harness) -> None:
    factory.gh.issue(5, "Elsewhere", repo="owner/other")
    url = factory.gh.issue(21, "Greeting", sub_issues=["owner/other#5"])
    before = factory.branches()

    result = factory.run(url)

    assert_refused(factory, result, before)
    assert result.err == f"factory: sub-issue owner/other#5 of {url} lives in another repository\n"


def test_github_access_error_stops_the_run_before_it_starts(factory: Harness) -> None:
    url = publish(factory)
    factory.gh.set(denied=True)
    before = factory.branches()

    result = factory.run(url)

    assert_refused(factory, result, before)
    assert result.err.startswith("factory: gh api failed: HTTP 401: Bad credentials")


def test_spec_link_that_is_not_a_github_issue_stops_the_run(factory: Harness) -> None:
    result = factory.run(f"https://github.com/{REPO}/pull/3")

    assert result.code == 2
    assert result.err == f"factory: https://github.com/{REPO}/pull/3 is not a link to a GitHub issue\n"


def test_failed_push_is_journaled_and_leaves_the_run_state_and_the_commands_to_land_it(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.reject_pushes()

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    integration = f"factory/{run_id}"
    [failed] = factory.events(run_id, "LandingFailed")
    assert failed["step"] == "push"
    assert "git push" in failed["reason"]
    assert factory.gh.pulls() == []
    body = factory.run_dir(run_id) / "pull_request.md"
    lines = result.out.splitlines()
    assert lines[-4:] == [
        f"Run {run_id} finished: done in {factory.run_took(run_id)}, $0.00",
        "Land it by hand:",
        f"  git push origin {integration}",
        f"  gh pr create --repo {REPO} --base main --head {integration} --title {TITLE} --body-file {body}",
    ]
    assert factory.cli("status", run_id).out.splitlines()[0].startswith(f"Run {run_id}  done  {integration}")


def test_push_without_a_remote_of_the_spec_repository_fails_the_landing_only(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.remove_remotes()

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    [failed] = factory.events(run_id_of(result.out), "LandingFailed")
    assert failed["reason"] == f"no git remote of {factory.repo} points at https://github.com/{REPO}.git"
    assert result.out.splitlines()[-2].startswith(f"  git push https://github.com/{REPO}.git factory/")


def test_code_host_leaves_a_remote_whose_url_tells_its_host_to_that_host(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.project["code_host"] = "gitlab"

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    [opened] = factory.events(run_id_of(result.out), "PullRequestOpened")
    assert opened["url"] == f"https://github.com/{REPO}/pull/1"


def test_run_tree_shows_each_ticket_by_its_issue_reference(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    rows = [line.split()[0] for line in result.out.splitlines() if line.startswith("#")]
    assert set(rows) == {"#28", "#30"}
