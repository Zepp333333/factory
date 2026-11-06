"""A Run waits for an event on the Tracker in the Run node `wait_tracker`, through the port member `events`."""

import contextlib
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from factory.adapters.github import GitHubPullRequests, Repository
from factory.adapters.gitlab import GitLabPullRequests
from factory.adapters.local_tracker import PullRequestFolder
from factory.adapters.tracker_cli import signed
from factory.ports.tracker import TrackerError
from fake_kinds import ACCOUNT
from harness import AgentScript, Harness, put_graph, run_id_of
from test_ci_wait import ci_run, pr_url
from test_recovery import STALE, crashed_run, killed_runs_run_no_handler  # noqa: F401
from test_run_graph import entered, greeting, walked
from test_tracker_operations import LINK, published
from test_tracker_run import Tracker, tracker  # noqa: F401

APPROVED = {"github": "APPROVED", "gitlab": "approved this merge request"}
CHANGES = {"github": "CHANGES_REQUESTED", "gitlab": "requested changes"}
ROUTES = {"approved": "accepted", "changes_requested": "refused", "commented": "talked", "labeled": "marked"}
PEER = "alice"
SAID = {"summary": "Greet", "handoff": "done"}


def said_on_the_spec(text: str) -> dict[str, Any]:
    return {"kind": "comment", "with": {"target": "spec", "text": text}, "next": {"done": "ended"}}


def graph(on_list: list[str], **with_: Any) -> dict[str, Any]:
    """Returns a Run graph that opens the PR, waits for `on_list` and goes to a node of its own for each."""
    routes = {event: ROUTES[event] for event in on_list}
    return {
        "start": "select",
        "escalate_to": "ended",
        "nodes": {
            "select": {"kind": "all", "next": {"selected": "build", "empty": "ended"}},
            "build": {
                "kind": "tickets",
                "with": {"pipeline": "${vars.pipeline}"},
                "next": {"all_merged": "open_pr", "some_merged": "open_pr", "none_merged": "ended"},
            },
            "open_pr": {"kind": "open_pr", "next": {"opened": "approval", "failed": "ended"}},
            "approval": {
                "kind": "wait_tracker",
                "with": {"on": on_list, "timeout": "1h", **with_},
                "next": {**routes, "timeout": "gave_up"},
            },
            **{route: said_on_the_spec(f"Went on as {route}.") for route in routes.values()},
            "gave_up": said_on_the_spec("Nobody answered."),
        },
    }


def when_waiting(factory: Harness, act: Callable[[], None]) -> None:
    """Calls `act` once the Run waits for the Tracker."""

    def on_sleep(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "TrackerWaitStarted"):
            factory.clock.on_sleep = None
            act()

    factory.clock.on_sleep = on_sleep


def ended(factory: Harness, run_id: str) -> list[dict[str, Any]]:
    return factory.events(run_id, "TrackerWaitEnded")


def review(factory: Harness, host: str, verdict: dict[str, str]) -> None:
    """Has a peer give a verdict on the Run's PR on `host`."""
    if host == "github":
        factory.gh.review(1, PEER, verdict[host])
    else:
        factory.glab.note_on_merge_request(1, PEER, verdict[host], system=True)


@pytest.mark.parametrize("host", ["github", "gitlab"])
@pytest.mark.parametrize(("verdict", "event"), [(APPROVED, "approved"), (CHANGES, "changes_requested")])
def test_a_verdict_on_the_pr_routes_the_run_to_the_node_of_its_event_and_holds_no_slot(
    factory: Harness, host: str, verdict: dict[str, str], event: str
) -> None:
    url = ci_run(factory, host)
    put_graph(factory, "approving", graph(["approved", "changes_requested"]))
    held = []

    def on_sleep(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "TrackerWaitStarted"):
            held.append(factory.slots())
            if len(held) == 1:
                review(factory, host, verdict)

    factory.clock.on_sleep = on_sleep

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-2:] == [("approval", event), (ROUTES[event], "done")]
    [heard] = ended(factory, run_id)
    assert heard["event"]["author"] == PEER
    assert factory.events(run_id, "TrackerWaitStarted")[0]["target"] == pr_url(host)
    assert held and all(slots == [] for slots in held)


def test_no_event_routes_timeout_as_declared_and_a_stranger_and_the_factory_count_for_nothing(
    factory: Harness,
) -> None:
    url = ci_run(factory)
    put_graph(factory, "approving", graph(["approved", "commented"], timeout="5m"))

    def act() -> None:
        factory.gh.review(1, "mallory", "APPROVED", association="NONE")
        factory.gh.write_on_pull(1, ACCOUNT, signed("The Run waits."))

    when_waiting(factory, act)

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-2:] == [("approval", "timeout"), ("gave_up", "done")]
    assert ended(factory, run_id)[0]["handled"] == []


def test_a_run_killed_while_waiting_resumes_in_the_node_and_sees_an_approval_given_meanwhile(factory: Harness) -> None:
    url = ci_run(factory)
    put_graph(factory, "approving", graph(["approved"], timeout="5m"))
    run_id = crashed_run(factory, url, lambda run_id: bool(factory.events(run_id, "TrackerWaitStarted")))
    factory.gh.review(1, PEER, "APPROVED")
    # The Run was orphaned past its `timeout`, and the approval came before it ran out.
    factory.clock.sleep(STALE.seconds + 600)
    assert "Run node: approval" in factory.cli("status", run_id).out.splitlines()

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert entered(factory, run_id).count("approval") == 1
    assert len(factory.events(run_id, "TrackerWaitStarted")) == 1
    assert walked(factory, run_id)[-2:] == [("approval", "approved"), ("accepted", "done")]


def test_a_run_killed_after_it_heard_the_event_keeps_it_on_resume(factory: Harness) -> None:
    url = ci_run(factory)
    put_graph(factory, "approving", graph(["approved"]))
    when_waiting(factory, lambda: factory.gh.review(1, PEER, "APPROVED"))
    run_id = run_id_of(factory.run(url).out)
    with contextlib.closing(sqlite3.connect(factory.home / "factory.db")) as db, db:
        last = db.execute(
            "SELECT MIN(seq) FROM events WHERE run_id = ? AND seq > (SELECT seq FROM events WHERE run_id = ?"
            " AND type = 'TrackerWaitEnded')",
            (run_id, run_id),
        ).fetchone()[0]
        db.execute("DELETE FROM events WHERE run_id = ? AND seq >= ?", (run_id, last))
        db.execute("UPDATE runs SET state = 'orphaned' WHERE id = ?", (run_id,))

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert walked(factory, run_id)[-2:] == [("approval", "approved"), ("accepted", "done")]
    assert len(factory.events(run_id, "TrackerWaitStarted")) == 1


def test_finish_ends_a_run_that_waits_for_the_tracker_as_any_run_that_merged_its_tickets(factory: Harness) -> None:
    url = ci_run(factory)
    put_graph(factory, "approving", graph(["approved"]))
    run_id = crashed_run(factory, url, lambda run_id: bool(factory.events(run_id, "TrackerWaitStarted")))
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("finish", run_id)

    assert result.code == 0, result.out + result.err
    assert f"Run {run_id} finished: done" in result.out


@pytest.mark.parametrize(
    ("act", "event"),
    [
        pytest.param(lambda gh: gh.write_on_pull(1, PEER, "Why?"), "commented", id="conversation"),
        pytest.param(lambda gh: gh.write_on_pull(1, PEER, "Why?", thread=True), "commented", id="thread"),
        pytest.param(lambda gh: gh.review(1, PEER, "COMMENTED", "Why?"), "commented", id="review_with_text"),
        pytest.param(lambda gh: gh.add_label(1, PEER, "ready", pull=True), "labeled", id="label"),
    ],
)
def test_a_comment_or_a_label_on_the_pr_routes_the_run(
    factory: Harness, act: Callable[[Any], None], event: str
) -> None:
    url = ci_run(factory)
    put_graph(factory, "approving", graph(["commented", "labeled"]))
    when_waiting(factory, lambda: act(factory.gh))

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    assert walked(factory, run_id_of(result.out))[-2] == ("approval", event)


@pytest.mark.parametrize("event", ["commented", "labeled"])
def test_a_comment_or_a_label_on_the_merge_request_routes_the_run_and_a_stranger_counts_for_nothing(
    factory: Harness, event: str
) -> None:
    url = ci_run(factory, "gitlab")
    put_graph(factory, "approving", graph(["commented", "labeled"]))

    def act() -> None:
        factory.glab.note_on_merge_request(1, "mallory", "Merge it.")
        factory.glab.note_on_merge_request(1, PEER, "unapproved this merge request", system=True)
        if event == "commented":
            factory.glab.note_on_merge_request(1, PEER, "Why?")
        else:
            factory.glab.add_label_to_merge_request(1, PEER, "ready")

    when_waiting(factory, act)

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    assert walked(factory, run_id_of(result.out))[-2] == ("approval", event)
    assert ended(factory, run_id_of(result.out))[0]["event"]["author"] == PEER


def test_of_two_verdicts_given_before_a_poll_the_later_one_routes_and_both_are_handled(factory: Harness) -> None:
    url = ci_run(factory)
    wait = graph(["approved", "changes_requested"])
    wait["nodes"]["refused"] = {
        "kind": "wait_tracker",
        "with": {"on": ["approved", "changes_requested"], "timeout": "5m"},
        "next": {"approved": "accepted", "changes_requested": "gave_up", "timeout": "gave_up"},
    }
    put_graph(factory, "approving", wait)

    def act() -> None:
        factory.gh.review(1, PEER, "APPROVED")
        factory.gh.review(1, "bob", "CHANGES_REQUESTED")

    when_waiting(factory, act)

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    # The second wait does not take the approval from before the request for changes.
    assert walked(factory, run_id_of(result.out))[-3:] == [
        ("approval", "changes_requested"),
        ("refused", "timeout"),
        ("gave_up", "done"),
    ]


def test_an_approval_given_while_the_run_waited_for_ci_counts(factory: Harness) -> None:
    url = ci_run(factory)
    wait = graph(["approved"])
    wait["nodes"]["open_pr"]["next"]["opened"] = "ci"
    wait["nodes"]["ci"] = {
        "kind": "wait_ci",
        "next": {"green": "approval", "absent": "approval", "red": "ended", "timeout": "ended"},
    }
    put_graph(factory, "approving", wait)
    factory.gh.checks([{"name": "unit", "bucket": "pending", "link": "https://ci"}], [])

    def on_sleep(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "CiStarted"):
            factory.clock.on_sleep = None
            factory.gh.review(1, PEER, "APPROVED")
            factory.gh.checks([{"name": "unit", "bucket": "pass", "link": "https://ci"}])

    factory.clock.on_sleep = on_sleep

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    assert walked(factory, run_id_of(result.out))[-3:] == [
        ("ci", "green"),
        ("approval", "approved"),
        ("accepted", "done"),
    ]


def test_a_wait_on_the_spec_hears_only_what_comes_after_it_began(tracker: Tracker) -> None:  # noqa: F811
    factory = tracker.factory
    url = tracker.publish()
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output=SAID)
    (factory.gh if tracker.name == "github" else factory.glab).say(21, PEER, "From before the wait.")
    put_graph(factory, "approving", on_the_spec(graph(["commented"], target="spec", timeout="10m")))
    when_waiting(factory, lambda: spec_comment(tracker, "From during the wait."))

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-2:] == [("approval", "commented"), ("talked", "done")]
    [started] = factory.events(run_id, "TrackerWaitStarted")
    assert started["target"] == "21"
    assert len(started["handled"]) >= 1


def test_a_label_on_the_spec_routes_the_run(factory: Harness) -> None:
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output=SAID)
    factory.gh.issue(28, "Greet", "Write hello.txt.")
    url = factory.gh.issue(21, "Greeting", "Say hello.", sub_issues=[28])
    put_graph(factory, "approving", on_the_spec(graph(["labeled"], target="spec")))
    when_waiting(factory, lambda: factory.gh.add_label(21, PEER, "ready"))

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    assert walked(factory, run_id_of(result.out))[-2] == ("approval", "labeled")


def on_the_spec(wait: dict[str, Any]) -> dict[str, Any]:
    """Has the Run graph wait on the Spec before it opens any PR."""
    wait["nodes"]["build"]["next"] = {"all_merged": "approval", "some_merged": "approval", "none_merged": "ended"}
    del wait["nodes"]["open_pr"]
    return wait


def spec_comment(on: Tracker, text: str) -> None:
    (on.factory.gh if on.name == "github" else on.factory.glab).say(21, PEER, text)


def test_a_tracker_that_cannot_be_read_holds_the_wait_past_its_timeout_and_the_event_still_counts(
    factory: Harness,
) -> None:
    url = ci_run(factory)
    put_graph(factory, "approving", graph(["approved"], timeout="2m"))

    def act() -> None:
        factory.gh.set(denied=True)

        def come_back(seconds: float) -> None:
            run_id = factory.run_ids()[-1]
            elapsed = factory.clock.now() - factory.times(run_id, "TrackerWaitStarted")[0]
            if elapsed.total_seconds() > 300:
                factory.clock.on_sleep = None
                factory.gh.set(denied=False)
                factory.gh.review(1, PEER, "APPROVED")

        factory.clock.on_sleep = come_back

    when_waiting(factory, act)

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "TrackerActionFailed")[0]["action"] == "events"
    assert walked(factory, run_id)[-2] == ("approval", "approved")


def test_the_project_file_sets_how_often_the_wait_reads_the_tracker(factory: Harness) -> None:
    url = ci_run(factory)
    factory.project["tracker_poll"] = "1h"
    put_graph(factory, "approving", graph(["approved"], timeout="1d"))
    when_waiting(factory, lambda: factory.gh.review(1, PEER, "APPROVED"))

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-2] == ("approval", "approved")
    took = factory.times(run_id, "TrackerWaitEnded")[0] - factory.times(run_id, "TrackerWaitStarted")[0]
    assert took.total_seconds() >= 3600


def test_a_run_whose_pr_is_a_file_times_out_at_once(factory: Harness) -> None:
    put_graph(factory, "approving", graph(["approved"], timeout="1d"))

    result = factory.run(greeting(factory))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-2:] == [("approval", "timeout"), ("gave_up", "done")]
    assert factory.events(run_id, "TrackerWaitStarted") == []


def test_a_pr_file_has_no_events(tmp_path: Path) -> None:
    assert PullRequestFolder(tmp_path).events(str(tmp_path / "pr.md")) == []


@pytest.mark.parametrize(
    ("pull_requests", "elsewhere"),
    [
        pytest.param(GitHubPullRequests(Repository("Owner", "target")), "https://example.com/1", id="github"),
        pytest.param(GitLabPullRequests("gitlab.example.com", "group/project"), "https://example.com/1", id="gitlab"),
    ],
)
def test_events_refuse_what_is_no_link_to_a_pr_of_the_code_host(pull_requests: Any, elsewhere: str) -> None:
    with pytest.raises(TrackerError, match="is not a link"):
        pull_requests.events(elsewhere)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param({"on": ["merged"]}, "has merged, and only", id="unknown_event"),
        pytest.param({"on": []}, "names no event", id="no_event"),
        pytest.param({"on": ["approved"], "target": "spec"}, "has approved, which only a PR gets", id="review_on_spec"),
        pytest.param({"target": "ticket"}, "target", id="unknown_target"),
        pytest.param({"timeout": "soon"}, "must be a duration", id="bad_timeout"),
        pytest.param({"timeout": None}, "missing timeout", id="no_timeout"),
        pytest.param({"on": None}, "missing on", id="no_on"),
    ],
)
def test_check_refuses_what_a_wait_tracker_node_says_of_itself(
    factory: Harness, change: dict[str, Any], expected: str
) -> None:
    wait = graph(["commented"])
    with_ = wait["nodes"]["approval"]["with"]
    with_.update(change)
    for key in [key for key, value in change.items() if value is None]:
        del with_[key]
    put_graph(factory, "approving", wait)
    factory.write_spec("Greeting", "Say hello.", {"t1": "---\ntitle: Greet\n---\nWrite hello.txt.\n"})

    result = factory.check()

    assert result.code == 2
    assert expected in result.err


def test_check_refuses_a_wait_with_an_event_it_does_not_route_or_one_on_the_pr_before_any_pr(factory: Harness) -> None:
    wait = graph(["commented"])
    del wait["nodes"]["approval"]["next"]["commented"]
    put_graph(factory, "approving", wait)
    assert "outcome commented has no route" in factory.check().err

    wait = graph(["commented"])
    wait["nodes"]["build"]["next"] = {"all_merged": "approval", "some_merged": "approval", "none_merged": "ended"}
    del wait["nodes"]["open_pr"]
    put_graph(factory, "approving", wait)
    assert "a path reaches this wait_tracker node before the open_pr node" in factory.check().err


def test_a_wait_on_the_spec_cannot_start_on_a_tracker_without_discussions(factory: Harness) -> None:
    published(factory)
    put_graph(factory, "approving", on_the_spec(graph(["commented"], target="spec")))

    result = factory.run(LINK, "--tickets", "1")

    assert result.code == 2
    roles = "through the roles Discussions and TicketBoard"
    assert f"the node approval waits for an event on the Spec {roles}, which the Tracker of {LINK}" in result.err


def test_a_project_file_with_a_bad_tracker_poll_fails_check(factory: Harness) -> None:
    factory.project["tracker_poll"] = "soon"

    result = factory.check()

    assert result.code == 2
    assert "tracker_poll" in result.err
