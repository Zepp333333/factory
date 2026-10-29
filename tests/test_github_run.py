import json
import shutil
import sqlite3
import sys
from typing import Any

from harness import REPO, AgentScript, CliResult, FakeTty, Harness, run_id_of
from test_human_decider import asking

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


def test_run_reads_the_spec_its_sub_issues_and_their_blockers_into_the_local_snapshot_shape(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [snapshotted] = factory.events(run_id, "SpecSnapshotted")
    assert snapshotted["snapshot"] == {
        "ref": url,
        "title": "Greeting",
        "body": "Say hello.",
        "tickets": [
            {"id": "28", "title": "Greet", "body": "Write hello.txt.", "blocked_by": []},
            {"id": "30", "title": "Greet again", "body": "Write bye.txt.", "blocked_by": ["28"]},
        ],
    }
    run_dir = factory.run_dir(run_id)
    assert (run_dir / "spec.md").read_text() == "# Greeting\n\nSay hello.\n"
    assert (run_dir / "tickets" / "30" / "ticket.md").read_text() == "# Greet again\nBlocked by: 28\n\nWrite bye.txt.\n"
    claimed = [event["branch"] for event in factory.events(run_id, "TicketClaimed")]
    assert claimed == [f"factory/{run_id}--28", f"factory/{run_id}--30"]


def test_run_that_merged_tickets_pushes_the_integration_branch_and_opens_a_pr_closing_them(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    integration = f"factory/{run_id}"
    assert factory.remote_branches() == {integration: factory.git("rev-parse", integration)}
    [pull] = factory.gh.pulls()
    assert pull == {
        "repo": REPO,
        "base": "main",
        "head": integration,
        "title": "Greeting: Greet, Greet again",
        "body": f"Lands Spec #21 from Run {run_id}.\n\nCloses #28\nCloses #30\n",
    }
    [pushed] = factory.events(run_id, "IntegrationBranchPushed")
    assert pushed == {"remote": "origin", "branch": integration, "commit": factory.git("rev-parse", integration)}
    [opened] = factory.events(run_id, "PullRequestOpened")
    assert opened["url"] == f"https://github.com/{REPO}/pull/1"
    assert result.out.splitlines()[-2:] == [
        f"Run {run_id} finished: done in {factory.run_took(run_id)}, $0.00",
        f"Pull request: https://github.com/{REPO}/pull/1",
    ]


def test_pr_lists_the_abandoned_tickets_and_those_they_left_waiting(factory: Harness) -> None:
    factory.gh.issue(28, "Greet", "Write hello.txt.")
    factory.gh.issue(30, "Greet again", "Write bye.txt.", blocked_by=[28])
    factory.gh.issue(31, "Wave", "Write wave.txt.")
    url = factory.gh.issue(21, "Greeting", "Say hello.", sub_issues=[28, 30, 31])
    factory.executor.scripts["implement"] = APPEND
    factory.pipeline["defaults"]["sandbox_retries"] = 0
    factory.sandbox.refuse = 1

    result = factory.run(url)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    [pull] = factory.gh.pulls()
    assert pull["body"] == (
        f"Lands Spec #21 from Run {run_id}.\n\n"
        "Closes #31\n\n"
        "Not landed:\n\n"
        "- #28 Greet: abandoned, blocking #30\n"
        "- #30 Greet again: not started, blocked by #28\n"
    )
    assert factory.cli("status", run_id).out.splitlines()[0].startswith(f"Run {run_id}  partial  factory/{run_id}")


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


def test_tickets_flag_takes_only_the_listed_sub_issues(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url, "--tickets", "28")

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [snapshotted] = factory.events(run_id, "SpecSnapshotted")
    assert [ticket["id"] for ticket in snapshotted["snapshot"]["tickets"]] == ["28"]
    assert factory.gh.pulls()[0]["body"] == f"Lands Spec #21 from Run {run_id}.\n\nCloses #28\n"


def test_ticket_that_is_not_a_sub_issue_of_the_spec_stops_the_run(factory: Harness) -> None:
    url = publish(factory)
    before = factory.branches()

    result = factory.run(url, "--tickets", "28,27")

    assert_refused(factory, result, before)
    assert result.err == f"factory: #27 is not a sub-issue of {url}\n"


def test_tickets_flag_takes_a_repeated_number_once(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url, "--tickets", "28,28")

    [snapshotted] = factory.events(run_id_of(result.out), "SpecSnapshotted")
    assert [ticket["id"] for ticket in snapshotted["snapshot"]["tickets"]] == ["28"]


def test_closed_sub_issue_in_the_tickets_flag_stops_the_run(factory: Harness) -> None:
    factory.gh.issue(28, "Greet")
    factory.gh.issue(29, "Already done", state="closed")
    url = factory.gh.issue(21, "Greeting", sub_issues=[28, 29])
    before = factory.branches()

    result = factory.run(url, "--tickets", "28,29")

    assert_refused(factory, result, before)
    assert result.err == "factory: #29 is closed, so this Run cannot take it\n"


def test_tickets_flag_refuses_anything_but_issue_numbers(factory: Harness) -> None:
    url = publish(factory)

    result = factory.run(url, "--tickets", "28,#30")

    assert result.code == 2
    assert "--tickets" in result.err


def test_open_blocker_outside_the_selection_stops_the_run_before_it_starts(factory: Harness) -> None:
    url = publish(factory)
    before = factory.branches()

    result = factory.run(url, "--tickets", "30")

    assert_refused(factory, result, before)
    assert result.err == "factory: #30 is blocked by open #28, which this Run does not take\n"


def test_open_blocker_in_another_repository_stops_the_run(factory: Harness) -> None:
    factory.gh.issue(5, "Elsewhere", repo="owner/other")
    factory.gh.issue(28, "Greet", blocked_by=["owner/other#5"])
    url = factory.gh.issue(21, "Greeting", sub_issues=[28])
    before = factory.branches()

    result = factory.run(url)

    assert_refused(factory, result, before)
    assert result.err == "factory: #28 is blocked by open owner/other#5, which this Run does not take\n"


def test_closed_blocker_in_another_repository_counts_as_done(factory: Harness) -> None:
    factory.gh.issue(5, "Elsewhere", state="closed", repo="owner/other")
    factory.gh.issue(28, "Greet", blocked_by=["owner/other#5"])
    url = factory.gh.issue(21, "Greeting", sub_issues=[28])
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    [snapshotted] = factory.events(run_id_of(result.out), "SpecSnapshotted")
    assert snapshotted["snapshot"]["tickets"][0]["blocked_by"] == []


def test_run_without_the_flag_skips_closed_sub_issues(factory: Harness) -> None:
    factory.gh.issue(28, "Greet")
    factory.gh.issue(29, "Already done", state="closed")
    url = factory.gh.issue(21, "Greeting", sub_issues=[28, 29])
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url)

    [snapshotted] = factory.events(run_id_of(result.out), "SpecSnapshotted")
    assert [ticket["id"] for ticket in snapshotted["snapshot"]["tickets"]] == ["28"]


def test_spec_with_no_open_sub_issues_stops_the_run(factory: Harness) -> None:
    factory.gh.issue(29, "Already done", state="closed")
    url = factory.gh.issue(21, "Greeting", sub_issues=[29])
    before = factory.branches()

    result = factory.run(url)

    assert_refused(factory, result, before)
    assert result.err == f"factory: {url} has no open sub-issues\n"


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


def test_missing_spec_issue_stops_the_run_before_it_starts(factory: Harness) -> None:
    before = factory.branches()

    result = factory.run(f"https://github.com/{REPO}/issues/404")

    assert_refused(factory, result, before)
    assert result.err == "factory: gh api failed: gh: Not Found (HTTP 404)\n"


def test_spec_link_that_is_not_a_github_issue_stops_the_run(factory: Harness) -> None:
    result = factory.run(f"https://github.com/{REPO}/pull/3")

    assert result.code == 2
    assert result.err == f"factory: https://github.com/{REPO}/pull/3 is not a link to a GitHub issue\n"


def test_edits_and_closed_issues_during_the_run_do_not_reach_it(factory: Harness) -> None:
    url = publish(factory)
    state = factory.gh.root / "state.json"
    edit = (
        "import json, pathlib, sys; p = pathlib.Path(sys.argv[1]); d = json.loads(p.read_text()); "
        f"i = d['repos']['{REPO}']['issues']; i['21']['title'] = 'Edited'; i['30']['state'] = 'closed'; "
        "i['30']['body'] = 'Write nothing.'; p.write_text(json.dumps(d))"
    )
    factory.executor.scripts["implement"] = AgentScript(
        run=[f"{sys.executable} -c {json.dumps(edit)} {state}", "echo line >> log.txt"],
        output={"handoff": "appended"},
    )

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.gh.state()["repos"][REPO]["issues"]["21"]["title"] == "Edited"
    assert (factory.run_dir(run_id) / "tickets" / "30" / "ticket.md").read_text().endswith("Write bye.txt.\n")
    assert [event["ticket"] for event in factory.events(run_id, "TicketClaimed")] == ["28", "30"]
    [pull] = factory.gh.pulls()
    assert pull["title"] == "Greeting: Greet, Greet again"
    assert pull["body"].endswith("Closes #28\nCloses #30\n")
    assert len(factory.gh.calls("api")) == 4


def test_failed_push_is_journaled_and_leaves_the_run_state_and_the_commands_to_land_it(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND
    shutil.rmtree(factory.remote)

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


def test_failed_pr_is_journaled_and_leaves_the_run_state_and_the_command_to_open_it(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.gh.set(pr_fails=True)

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    integration = f"factory/{run_id}"
    assert integration in factory.remote_branches()
    [failed] = factory.events(run_id, "LandingFailed")
    assert failed == {
        "step": "pull_request",
        "reason": "gh pr failed: pull request create failed: GraphQL: Head sha can't be blank (createPullRequest)",
    }
    body = factory.run_dir(run_id) / "pull_request.md"
    assert result.out.splitlines()[-2:] == [
        "Land it by hand:",
        f"  gh pr create --repo {REPO} --base main --head {integration} --title {TITLE} --body-file {body}",
    ]


def test_push_goes_to_the_remote_of_the_spec_repository(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.git("remote", "rename", "origin", "upstream")
    factory.git("remote", "add", "origin", "git@github.com:someone/fork.git")

    result = factory.run(url)

    [pushed] = factory.events(run_id_of(result.out), "IntegrationBranchPushed")
    assert pushed["remote"] == "upstream"


def test_push_without_a_remote_of_the_spec_repository_fails_the_landing_only(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.git("remote", "remove", "origin")

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    [failed] = factory.events(run_id_of(result.out), "LandingFailed")
    assert failed["reason"] == f"no git remote of {factory.repo} points at https://github.com/{REPO}.git"
    assert result.out.splitlines()[-2].startswith(f"  git push https://github.com/{REPO}.git factory/")


def test_local_spec_records_its_pr_in_the_folder_and_never_calls_gh(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": "Write hello.txt.\n"})
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert not (factory.gh.root / "calls.jsonl").exists()
    assert factory.remote_branches() == {}
    [recorded] = (spec / "pull_requests").glob("*.md")
    assert f"Pull request: {recorded}" in result.out


def test_tickets_flag_needs_a_github_spec(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": "Write hello.txt.\n"})

    result = factory.run(spec, "--tickets", "1")

    assert result.code == 2
    assert result.err == "factory: --tickets takes sub-issues of a GitHub Spec, not Tickets of a folder\n"


def answer_cards(factory: Harness, tty: FakeTty, answered: set[int]) -> None:
    """Types `accept` once for each Ticket whose label the Tracker already holds, as the owner would on seeing it."""
    for number in (28, 30):
        if number not in answered and factory.gh.issue_state(number).get("labels"):
            answered.add(number)
            tty.type("accept\n")


def test_a_waiting_human_decision_labels_and_comments_the_issue_and_the_answer_unlabels_it(factory: Harness) -> None:
    asking(factory)
    url = publish(factory)
    seen: dict[str, Any] = {}
    answered: set[int] = set()
    tty = factory.stdin = FakeTty()

    def on_sleep(seconds: float) -> None:
        issue = factory.gh.issue_state(28)
        if issue.get("labels") and not seen:
            seen.update(labels=issue["labels"], comments=list(issue["comments"]))
        answer_cards(factory, tty, answered)

    factory.clock.on_sleep = on_sleep

    result = factory.run(url)

    tty.close()
    assert result.code == 0, result.out + result.err
    assert seen["labels"] == ["factory:needs-human"]
    assert len(seen["comments"]) == 1
    assert seen["comments"][0].startswith("# Decision judge of Ticket 28: Greet")
    issue = factory.gh.issue_state(28)
    assert issue["labels"] == []
    assert len(issue["comments"]) == 2
    assert "was decided: accept" in issue["comments"][1]


def test_closing_the_issue_of_a_waiting_ticket_does_not_affect_the_run(factory: Harness) -> None:
    asking(factory)
    url = publish(factory)
    tty = factory.stdin = FakeTty()
    closed = False
    answered: set[int] = set()

    def on_sleep(seconds: float) -> None:
        nonlocal closed
        if factory.gh.issue_state(28).get("labels") and not closed:
            closed = True
            factory.gh.edit(28, state="closed")
        answer_cards(factory, tty, answered)

    factory.clock.on_sleep = on_sleep

    result = factory.run(url)

    tty.close()
    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert closed
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"
    assert [event["ticket"] for event in factory.events(run_id, "TicketClaimed")] == ["28", "30"]
    assert factory.gh.issue_state(28)["labels"] == []


def test_an_unreachable_github_never_stops_the_run_and_the_projection_is_delivered_after_it_comes_back(
    factory: Harness,
) -> None:
    asking(factory)
    url = publish(factory)
    answered: set[int] = set()
    tty = factory.stdin = FakeTty()
    down = False

    def on_sleep(seconds: float) -> None:
        nonlocal down
        if factory.waiting() and not down and not factory.gh.issue_state(28).get("labels"):
            down = True
            factory.gh.set(denied=True)
        elif down and factory.events(factory.run_ids()[-1], "TrackerDeliveryFailed"):
            down = False
            factory.gh.set(denied=False)
            factory.clock.sleep(60)
        answer_cards(factory, tty, answered)

    factory.clock.on_sleep = on_sleep

    result = factory.run(url)

    tty.close()
    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "TrackerDeliveryFailed")
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"
    assert factory.gh.issue_state(28)["labels"] == []
    assert len(factory.gh.issue_state(28)["comments"]) == 2


def test_an_answer_to_an_issue_the_owner_already_unlabelled_still_publishes_the_outcome(factory: Harness) -> None:
    asking(factory)
    url = publish(factory)
    tty = factory.stdin = FakeTty()
    answered: set[int] = set()

    def on_sleep(seconds: float) -> None:
        if factory.gh.issue_state(28).get("labels") and 28 not in answered:
            answered.add(28)
            factory.gh.edit(28, labels=[])
            tty.type("accept\n")
        answer_cards(factory, tty, answered)

    factory.clock.on_sleep = on_sleep

    result = factory.run(url)

    tty.close()
    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert not factory.events(run_id, "TrackerDeliveryFailed")
    assert "was decided: accept" in factory.gh.issue_state(28)["comments"][1]
