"""Scenarios that GitHub and GitLab Trackers share, each run once per Tracker."""

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from harness import REPO, TICKET_FIELDS, AgentScript, FakeGh, FakeGlab, FakeTty, Harness, run_id_of
from test_github_run import APPEND, TITLE
from test_github_run import publish as publish_github
from test_gitlab_run import MR_REPO, PROJECT, landing
from test_human_decider import asking


@dataclass
class Tracker:
    """Tracker gives a test the same handles on the fake `gh` and the fake `glab`.

    `publish` publishes Spec #21 whose Ticket #30 waits on #28, and lands through a remote of the Spec's project.
    `issues` is the Python expression that finds the issues in the fake's state `d`, for an agent that edits them.
    `pr_fails` is the key of the fake's state that makes it refuse to open the PR.
    """

    name: str
    factory: Harness
    fake: FakeGh | FakeGlab
    host: str
    pr_repo: str
    pr_fails: str
    publish: Callable[[], str]
    issues: str
    comments: str
    pr_url: str
    by_hand: Callable[[str, Path], str]
    pr_failed: str

    def ticket(self, number: int, title: str, body: str = "", state: str = "open", **links: Any) -> None:
        if self.name == "github":
            self.factory.gh.issue(number, title, body, state=state, **links)
        else:
            self.factory.glab.issue(number, title, body, state="opened" if state == "open" else state, **links)

    def spec(self, number: int, title: str, tickets: list[int], body: str = "") -> str:
        if self.name == "github":
            return self.factory.gh.issue(number, title, body, sub_issues=list[int | str](tickets))
        return self.factory.glab.issue(number, title, body, kind="Issue", children=tickets)

    def issue_state(self, number: int) -> dict[str, Any]:
        return self.fake.issue_state(number)

    def edit(self, number: int, **fields: Any) -> None:
        self.fake.edit(number, **fields)

    def set(self, **values: Any) -> None:
        self.fake.set(**values)

    def state_file(self) -> Path:
        return self.fake.root / "state.json"

    def pulls(self) -> list[dict[str, Any]]:
        return self.factory.gh.pulls() if self.name == "github" else self.factory.glab.merge_requests()


def github(factory: Harness) -> Tracker:
    return Tracker(
        name="github",
        factory=factory,
        fake=factory.gh,
        host="github.com",
        pr_repo=REPO,
        pr_fails="pr_fails",
        publish=lambda: publish_github(factory),
        issues=f"d['repos']['{REPO}']['issues']",
        comments="comments",
        pr_url=f"https://github.com/{REPO}/pull/1",
        by_hand=lambda head, body: (
            f"  gh pr create --repo {REPO} --base main --head {head} --title {TITLE} --body-file {body}\n"
        ),
        pr_failed="gh pr failed: pull request create failed: GraphQL: Head sha can't be blank (createPullRequest)",
    )


def gitlab(factory: Harness) -> Tracker:
    return Tracker(
        name="gitlab",
        factory=factory,
        fake=factory.glab,
        host="gitlab.example.com",
        pr_repo=MR_REPO,
        pr_fails="mr_fails",
        publish=lambda: landing(factory),
        issues=f"d['hosts']['gitlab.example.com']['projects']['{PROJECT}']['items']",
        comments="notes",
        pr_url=f"https://{MR_REPO}/-/merge_requests/1",
        by_hand=lambda head, body: (
            f"  glab mr create --repo {MR_REPO} --source-branch {head} --target-branch main --title {TITLE} "
        ),
        pr_failed="glab mr failed: ",
    )


@pytest.fixture(params=[github, gitlab], ids=["github", "gitlab"])
def tracker(request: pytest.FixtureRequest, factory: Harness) -> Tracker:
    made: Tracker = request.param(factory)
    return made


def publish_with_closed_tickets(tracker: Tracker) -> str:
    """Publishes Spec #21 with open #28 and #30, a closed #29, and on GitLab a child that is no Task.

    #28 waits on the closed #27 and #30 on #28, by native links on GitHub and front matter on GitLab.
    """
    if tracker.name == "github":
        gh = tracker.factory.gh
        gh.issue(27, "Lay the ground", state="closed")
        gh.issue(28, "Greet", "Write hello.txt.", blocked_by=[27])
        gh.issue(29, "Already done", state="closed")
        gh.issue(30, "Greet again", "Write bye.txt.", blocked_by=[28])
        return gh.issue(21, "Greeting", "Say hello.", sub_issues=[28, 29, 30])
    landing(tracker.factory)
    glab = tracker.factory.glab
    glab.issue(29, "Already done", state="closed")
    glab.issue(31, "A sub-issue", kind="Issue")
    return glab.issue(21, "Greeting", "Say hello.", kind="Issue", children=[28, 29, 30, 31])


def test_run_snapshots_the_open_tickets_once_and_lands_a_pr_that_closes_them_through_the_specs_remote(
    tracker: Tracker,
) -> None:
    factory = tracker.factory
    url = publish_with_closed_tickets(tracker)
    factory.git("remote", "rename", "origin", "upstream")
    factory.git("remote", "add", "origin", f"git@{tracker.host}:someone/fork.git")
    edit = (
        "import json, pathlib, sys; p = pathlib.Path(sys.argv[1]); d = json.loads(p.read_text()); "
        f"i = {tracker.issues}; i['21']['title'] = 'Edited'; i['30']['state'] = 'closed'; "
        "i['30']['body'] = 'Write nothing.'; p.write_text(json.dumps(d))"
    )
    factory.executor.scripts["implement"] = AgentScript(
        run=[f"{sys.executable} -c {json.dumps(edit)} {tracker.state_file()}", "echo line >> log.txt"],
        output={"summary": "Append a line", "handoff": "appended"},
    )

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    assert result.err == ""
    run_id = run_id_of(result.out)
    [snapshotted] = factory.events(run_id, "SpecSnapshotted")
    assert snapshotted["snapshot"] == {
        "ref": url,
        "title": "Greeting",
        "body": "Say hello.",
        "tickets": [
            {**TICKET_FIELDS, "id": "28", "title": "Greet", "body": "Write hello.txt."},
            {**TICKET_FIELDS, "id": "30", "title": "Greet again", "body": "Write bye.txt.", "blocked_by": ["28"]},
        ],
    }
    run_dir = factory.run_dir(run_id)
    assert (run_dir / "spec.md").read_text() == "# Greeting\n\nSay hello.\n"
    assert (run_dir / "tickets" / "30" / "ticket.md").read_text() == "# Greet again\nBlocked by: 28\n\nWrite bye.txt.\n"
    claimed = [event["branch"] for event in factory.events(run_id, "TicketClaimed")]
    assert claimed == [f"factory/{run_id}--28", f"factory/{run_id}--30"]
    assert tracker.issue_state(21)["title"] == "Edited"

    integration = f"factory/{run_id}"
    assert factory.remote_branches() == {integration: factory.git("rev-parse", integration)}
    [pushed] = factory.events(run_id, "IntegrationBranchPushed")
    assert pushed == {"remote": "upstream", "branch": integration, "commit": factory.git("rev-parse", integration)}
    [pull] = tracker.pulls()
    assert pull == {
        "repo": tracker.pr_repo,
        "base": "main",
        "head": integration,
        "title": "Greeting: Greet, Greet again",
        "body": f"Lands Spec #21 from Run {run_id}.\n\nCloses #28\nCloses #30\n",
    }
    [opened] = factory.events(run_id, "PullRequestOpened")
    assert opened["url"] == tracker.pr_url
    assert result.out.splitlines()[-2:] == [
        f"Run {run_id} finished: done in {factory.run_took(run_id)}, $0.00, no CI check within the grace",
        f"Pull request: {tracker.pr_url}",
    ]


def test_pr_lists_the_abandoned_tickets_and_those_they_left_waiting(tracker: Tracker) -> None:
    factory = tracker.factory
    if tracker.name == "gitlab":
        landing(factory)
        factory.glab.set(linked_items=True)
    tracker.ticket(28, "Greet", "Write hello.txt.")
    tracker.ticket(30, "Greet again", "Write bye.txt.", blocked_by=[28])
    tracker.ticket(31, "Wave", "Write wave.txt.")
    url = tracker.spec(21, "Greeting", [28, 30, 31], "Say hello.")
    factory.executor.scripts["implement"] = APPEND
    factory.pipeline["defaults"]["sandbox_retries"] = 0
    factory.sandbox.refuse = 1

    result = factory.run(url)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    [pull] = tracker.pulls()
    assert pull["body"] == (
        f"Lands Spec #21 from Run {run_id}.\n\n"
        "Closes #31\n\n"
        "Not landed:\n\n"
        "- #28 Greet: abandoned, blocking #30\n"
        "- #30 Greet again: not started, blocked by #28\n"
    )
    assert factory.cli("status", run_id).out.splitlines()[0].startswith(f"Run {run_id}  partial  factory/{run_id}")


def test_failed_pr_is_journaled_and_leaves_the_run_done_with_the_command_to_open_it(tracker: Tracker) -> None:
    factory = tracker.factory
    url = tracker.publish()
    factory.executor.scripts["implement"] = APPEND
    tracker.set(**{tracker.pr_fails: True})

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    integration = f"factory/{run_id}"
    assert integration in factory.remote_branches()
    [failed] = factory.events(run_id, "LandingFailed")
    assert failed["step"] == "pull_request"
    assert failed["reason"].startswith(tracker.pr_failed)
    by_hand = result.out.partition("Land it by hand:\n")[2]
    assert by_hand.startswith(tracker.by_hand(integration, factory.run_dir(run_id) / "pull_request.md"))
    assert "Pull request:" not in result.out
    assert factory.cli("status", run_id).out.splitlines()[0].startswith(f"Run {run_id}  done  {integration}")


def answer_cards(tracker: Tracker, tty: FakeTty, answered: set[int]) -> None:
    """Types `accept` once for each Ticket whose label the Tracker already holds, as the owner would on seeing it."""
    for number in (28, 30):
        if number not in answered and tracker.issue_state(number).get("labels"):
            answered.add(number)
            tty.type("accept\n")


def test_a_waiting_human_decision_is_projected_onto_its_issue_whatever_the_owner_does_to_the_issue(
    tracker: Tracker,
) -> None:
    factory = tracker.factory
    asking(factory)
    url = tracker.publish()
    seen: dict[str, Any] = {}
    answered: set[int] = set()
    tty = factory.stdin = FakeTty()

    def on_sleep(seconds: float) -> None:
        first = tracker.issue_state(28)
        if first.get("labels") and 28 not in answered:
            answered.add(28)
            tracker.edit(28, state="closed", labels=[])
            tty.type("accept\n")
        second = tracker.issue_state(30)
        if second.get("labels") and not seen:
            seen.update(labels=second["labels"], comments=list(second[tracker.comments]))
        answer_cards(tracker, tty, answered)

    factory.clock.on_sleep = on_sleep

    result = factory.run(url)

    tty.close()
    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"
    assert [event["ticket"] for event in factory.events(run_id, "TicketClaimed")] == ["28", "30"]
    assert not factory.events(run_id, "TrackerDeliveryFailed")
    assert seen["labels"] == ["factory:needs-human"]
    assert len(seen["comments"]) == 1
    assert seen["comments"][0].startswith("# Decision judge of Ticket 30: Greet again")
    for number in (28, 30):
        issue = tracker.issue_state(number)
        assert issue["labels"] == []
        assert len(issue[tracker.comments]) == 2
        assert "was decided: accept" in issue[tracker.comments][1]


def test_an_unreachable_tracker_never_stops_the_run_and_the_projection_is_delivered_after_it_comes_back(
    tracker: Tracker,
) -> None:
    factory = tracker.factory
    asking(factory)
    url = tracker.publish()
    answered: set[int] = set()
    tty = factory.stdin = FakeTty()
    down = False

    def on_sleep(seconds: float) -> None:
        nonlocal down
        if factory.waiting() and not down and not tracker.issue_state(28).get("labels"):
            down = True
            tracker.set(denied=True)
        elif down and factory.events(factory.run_ids()[-1], "TrackerDeliveryFailed"):
            down = False
            tracker.set(denied=False)
            factory.clock.sleep(60)
        answer_cards(tracker, tty, answered)

    factory.clock.on_sleep = on_sleep

    result = factory.run(url)

    tty.close()
    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "TrackerDeliveryFailed")
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"
    assert tracker.issue_state(28)["labels"] == []
    assert len(tracker.issue_state(28)[tracker.comments]) == 2


REFUSALS = {
    "closed": {
        "github": "factory: #29 is closed, so this Run cannot take it\n",
        "gitlab": "factory: #29 is closed, so this Run cannot take it\n",
    },
    "blocker": {
        "github": "factory: #30 is blocked by open #28, which this Run does not take\n",
        "gitlab": "factory: #30 is blocked by open #28, which this Run does not take\n",
    },
    "no open": {
        "github": "factory: {url} has no open sub-issues\n",
        "gitlab": "factory: {url} has no open Tickets\n",
    },
    "missing": {
        "github": "factory: gh api failed: gh: Not Found (HTTP 404)\n",
        "gitlab": f"is not an issue of {PROJECT}",
    },
}


def assert_refused(tracker: Tracker, result: Any, before: dict[str, str], message: str) -> None:
    factory = tracker.factory
    assert result.code == 2
    assert "started" not in result.out
    assert factory.branches() == before
    assert not (factory.home / "factory.db").exists() or factory.run_ids() == []
    if message.startswith("factory: "):
        assert result.err == message
    else:
        assert message in result.err


def test_closed_ticket_in_the_tickets_flag_stops_the_run(tracker: Tracker) -> None:
    tracker.ticket(28, "Greet")
    tracker.ticket(29, "Already done", state="closed")
    url = tracker.spec(21, "Greeting", [28, 29])
    before = tracker.factory.branches()

    result = tracker.factory.run(url, "--tickets", "28,29")

    assert_refused(tracker, result, before, REFUSALS["closed"][tracker.name])


def test_open_blocker_outside_the_selection_stops_the_run_before_it_starts(tracker: Tracker) -> None:
    url = tracker.publish()
    before = tracker.factory.branches()

    result = tracker.factory.run(url, "--tickets", "30")

    assert_refused(tracker, result, before, REFUSALS["blocker"][tracker.name])


def test_spec_with_no_open_tickets_stops_the_run(tracker: Tracker) -> None:
    tracker.ticket(29, "Already done", state="closed")
    url = tracker.spec(21, "Greeting", [29])
    before = tracker.factory.branches()

    result = tracker.factory.run(url)

    assert_refused(tracker, result, before, REFUSALS["no open"][tracker.name].format(url=url))


def test_missing_spec_issue_stops_the_run_before_it_starts(tracker: Tracker) -> None:
    tracker.publish()
    link = (
        f"https://github.com/{REPO}/issues/404"
        if tracker.name == "github"
        else f"https://gitlab.example.com/{PROJECT}/-/issues/404"
    )
    before = tracker.factory.branches()

    result = tracker.factory.run(link)

    assert_refused(tracker, result, before, REFUSALS["missing"][tracker.name])
