"""The Operations `assign`, `label` and `comment` act through the TicketBoard, on a Ticket or on the Run's Tickets."""

from collections.abc import Callable
from typing import Any

import pytest

from factory.config import display_path
from factory.ports.kind import Kind
from factory.ports.tracker import SpecLink, TicketSnapshot, Tracker
from fake_kinds import ACCOUNT, SPEC_ID, Notebook, NotebookTracker
from harness import AgentScript, Harness, put_graph, run_id_of
from test_commands import Owner
from test_recovery import STALE, Crash, killed_runs_run_no_handler  # noqa: F401
from test_run_graph import walked

LINK = "notebook:greeting"
FAILED_WORK = AgentScript(fail="crash")


def published(factory: Harness, **fields: Any) -> Notebook:
    """Publishes Spec `Greeting` with the Tickets 1 to 3 on a Notebook, which the Harness takes as a Tracker kind."""
    tickets = [
        TicketSnapshot("1", "Greet", "Write 1.txt."),
        TicketSnapshot("2", "Greet again", "Write 2.txt."),
        TicketSnapshot("3", "Greet later", "Write 3.txt."),
    ]
    for ticket in tickets:
        # Two scripts each, since a resumed Run starts the Attempt it lost again.
        script = AgentScript(files={f"{ticket.id}.txt": "hello\n"}, output={"summary": ticket.title, "handoff": "done"})
        factory.executor.ticket_scripts[(ticket.id, "implement")] = [script, script]
    notebook = Notebook(LINK, "Greeting", "Say hello.", tickets, **fields)
    factory.registries.trackers.register("notebook", notebook.kind())
    return notebook


def graph_with(claim: dict[str, Any]) -> dict[str, Any]:
    """Returns a Run graph that does the node `claim` first, then only builds, so its Run opens no PR."""
    return {
        "start": "claim",
        "escalate_to": "ended",
        "nodes": {
            "claim": {"next": {"done": "build"}, **claim},
            "build": {
                "kind": "tickets",
                "with": {"pipeline": "${vars.pipeline}"},
                "next": {"all_merged": "ended", "some_merged": "ended", "none_merged": "ended"},
            },
        },
    }


def acted(factory: Harness, run_id: str) -> list[tuple[str, str]]:
    """Returns the target and the action of each action of a Tracker Operation that went through, in order."""
    return [(e["target"], e["action"]) for e in factory.events(run_id, "TrackerActed")]


def on_the_way_out(factory: Harness, operation: dict[str, Any]) -> None:
    """Sends a Ticket whose Stage fails through the Operation `tell` on its way to `abandoned`."""
    factory.pipeline["operations"]["tell"] = {"next": {"done": "abandoned"}, **operation}
    factory.pipeline["escalate_to"] = "tell"


def test_a_run_graph_with_an_assign_node_assigns_every_ticket_it_took_before_the_first_attempt(
    factory: Harness,
) -> None:
    notebook = published(factory, assignees={"1": ["mona"], "2": [ACCOUNT]})
    put_graph(factory, "default", graph_with({"kind": "assign"}))

    result = factory.run(LINK, "--tickets", "1,2")

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert notebook.assignees == {"1": ["mona", ACCOUNT], "2": [ACCOUNT]}
    assert [(e["target"], e["result"]) for e in factory.events(run_id, "TrackerActed")] == [("1", ACCOUNT), ("2", None)]
    kinds = [kind for kind, _ in factory.all_events(run_id)]
    assert kinds.index("TrackerActed") < kinds.index("AttemptStarted")
    assert walked(factory, run_id)[0] == ("claim", "done")


def test_the_default_run_graph_assigns_nothing(factory: Harness) -> None:
    notebook = published(factory)

    result = factory.run(LINK, "--tickets", "1")

    assert result.code == 0, result.out + result.err
    assert notebook.assignees == {}
    assert factory.events(run_id_of(result.out), "TrackerActed") == []


def test_a_run_graph_labels_every_ticket_it_took_and_takes_labels_off(factory: Harness) -> None:
    notebook = published(factory, labels={"1": ["ready"], "2": ["ready", "other"]})
    node = {"kind": "label", "with": {"add": ["in-progress"], "remove": ["ready"]}}
    put_graph(factory, "default", graph_with(node))

    result = factory.run(LINK, "--tickets", "1,2")

    assert result.code == 0, result.out + result.err
    assert notebook.labels == {"1": ["in-progress"], "2": ["other", "in-progress"]}
    assert acted(factory, run_id_of(result.out)) == [
        ("1", "add:in-progress"),
        ("1", "remove:ready"),
        ("2", "add:in-progress"),
        ("2", "remove:ready"),
    ]


def test_a_run_graph_comments_on_every_ticket_with_the_references_of_the_run(factory: Harness) -> None:
    notebook = published(factory)
    text = "Run ${run} on ${branch} takes ${ticket} of ${spec}."
    put_graph(factory, "default", graph_with({"kind": "comment", "with": {"text": text}}))

    result = factory.run(LINK, "--tickets", "1,2")

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert notebook.comments == {
        "1": [f"Run {run_id} on factory/{run_id} takes #1 of #{SPEC_ID}."],
        "2": [f"Run {run_id} on factory/{run_id} takes #2 of #{SPEC_ID}."],
    }


@pytest.mark.parametrize(
    ("node", "seen"),
    [
        pytest.param({"kind": "assign", "with": {"target": "spec"}}, "assignees", id="assign"),
        pytest.param({"kind": "label", "with": {"add": ["running"], "target": "spec"}}, "labels", id="label"),
        pytest.param({"kind": "comment", "with": {"text": "Run ${run}.", "target": "spec"}}, "comments", id="comment"),
    ],
)
def test_a_run_node_with_target_spec_acts_on_the_spec_and_on_no_ticket(
    factory: Harness, node: dict[str, Any], seen: str
) -> None:
    notebook = published(factory)
    put_graph(factory, "default", graph_with(node))

    result = factory.run(LINK, "--tickets", "1")

    assert result.code == 0, result.out + result.err
    assert list(getattr(notebook, seen)) == [SPEC_ID]
    assert [target for target, _ in acted(factory, run_id_of(result.out))] == [SPEC_ID]


def test_a_ticket_pipeline_labels_its_ticket_needs_human_before_abandoned(factory: Harness) -> None:
    notebook = published(factory)
    factory.executor.ticket_scripts[("1", "implement")] = [FAILED_WORK]
    on_the_way_out(factory, {"kind": "label", "with": {"add": ["needs-human"]}})

    result = factory.run(LINK, "--tickets", "1,2")

    run_id = run_id_of(result.out)
    assert notebook.labels == {"1": ["needs-human"]}
    states = {e["ticket"]: e["node"] for e in factory.events(run_id, "NodeEntered")}
    assert states == {"1": "abandoned", "2": "done"}
    finished = factory.events(run_id, "OperationFinished")
    assert {"ticket": "1", "node": "tell", "kind": "label", "outcome": "done"}.items() <= finished[0].items()


def test_a_ticket_pipeline_assigns_and_comments_on_its_own_ticket(factory: Harness) -> None:
    notebook = published(factory)
    operations = factory.pipeline["operations"]
    operations["take"] = {"kind": "assign", "next": {"done": "tell"}}
    operations["tell"] = {
        "kind": "comment",
        "with": {"text": "${ticket} merges in ${run}."},
        "next": {"done": "catch_up"},
    }
    factory.pipeline["stages"]["implement"]["next"]["done"] = "take"

    result = factory.run(LINK, "--tickets", "1")

    assert result.code == 0, result.out + result.err
    assert notebook.assignees == {"1": [ACCOUNT]}
    assert notebook.comments == {"1": [f"#1 merges in {run_id_of(result.out)}."]}


def test_a_ticket_that_comes_back_to_an_operation_acts_again(factory: Harness) -> None:
    notebook = published(factory)
    factory.pipeline["operations"]["tell"] = {
        "kind": "comment",
        "with": {"text": "Done once more."},
        "next": {"done": "again"},
    }
    factory.pipeline["stages"]["implement"]["next"]["done"] = "tell"
    factory.pipeline["stages"]["implement"]["rounds"] = {"max": 2, "then": "catch_up"}
    factory.pipeline["gates"] = {
        "again": {
            "kind": "command",
            "with": {"run": "test -f again"},
            "next": {"pass": "catch_up", "fail": "implement"},
        }
    }
    script = AgentScript(output={"summary": "Again", "handoff": "done"})
    factory.executor.ticket_scripts[("1", "implement")] = [script, script]

    result = factory.run(LINK, "--tickets", "1")

    assert result.code == 0, result.out + result.err
    assert notebook.comments == {"1": ["Done once more.", "Done once more."]}


def test_a_run_killed_after_half_the_assignments_assigns_the_rest_on_resume_and_none_twice(factory: Harness) -> None:
    calls: list[str] = []

    def crash_at_the_second(member: str, ticket: str) -> None:
        calls.append(ticket)
        if len(calls) == 2:
            calls.pop()
            raise Crash

    notebook = published(factory, on_board_call=crash_at_the_second)
    put_graph(factory, "default", graph_with({"kind": "assign"}))

    with pytest.raises(Crash):
        factory.run(LINK)
    factory.crashed()
    run_id = factory.run_ids()[-1]
    assert notebook.assignees == {"1": [ACCOUNT]}
    assert factory.events(run_id, "AttemptStarted") == []
    factory.clock.sleep(STALE.seconds)
    notebook.on_board_call = lambda member, ticket: calls.append(ticket)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert calls == ["1", "2", "3"]
    assert notebook.assignees == {"1": [ACCOUNT], "2": [ACCOUNT], "3": [ACCOUNT]}
    assert [target for target, _ in acted(factory, run_id)] == ["1", "2", "3"]


def test_a_ticket_killed_between_the_actions_of_an_operation_does_only_the_rest_on_resume(factory: Harness) -> None:
    calls: list[str] = []

    def crash_at_the_second(member: str, ticket: str) -> None:
        calls.append(member)
        if len(calls) == 2:
            calls.pop()
            raise Crash

    notebook = published(factory, on_board_call=crash_at_the_second)
    factory.pipeline["operations"]["tell"] = {
        "kind": "label",
        "with": {"add": ["a"], "remove": ["b"]},
        "next": {"done": "catch_up"},
    }
    factory.pipeline["stages"]["implement"]["next"]["done"] = "tell"

    with pytest.raises(Crash):
        factory.run(LINK, "--tickets", "1")
    factory.crashed()
    run_id = factory.run_ids()[-1]
    factory.clock.sleep(STALE.seconds)
    notebook.on_board_call = lambda member, ticket: calls.append(member)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert calls == ["add_label", "remove_label"]
    assert notebook.labels == {"1": ["a"]}


def test_on_fail_warn_at_a_run_node_journals_shows_and_goes_on_to_the_next_ticket(factory: Harness) -> None:
    notebook = published(factory, board_fails=1)
    put_graph(factory, "default", graph_with({"kind": "assign"}))

    result = factory.run(LINK, "--tickets", "1,2")

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [
        (e["target"], e["action"], e["on_fail"], e["reason"]) for e in factory.events(run_id, "TrackerActionFailed")
    ] == [("1", "assign", "warn", "the Tracker is down")]
    assert "factory: warning: #1: assign failed: the Tracker is down" in result.out
    assert notebook.assignees == {"2": [ACCOUNT]}
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"


def test_a_warned_failure_is_a_result_that_a_resume_does_not_try_again(factory: Harness) -> None:
    def crash_after_the_warning(member: str, ticket: str) -> None:
        raise Crash

    notebook = published(factory, board_fails=1, on_board_call=crash_after_the_warning)
    put_graph(factory, "default", graph_with({"kind": "assign"}))

    with pytest.raises(Crash):
        factory.run(LINK, "--tickets", "1,2")
    factory.crashed()
    run_id = factory.run_ids()[-1]
    factory.clock.sleep(STALE.seconds)
    notebook.on_board_call = None

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert notebook.assignees == {"2": [ACCOUNT]}


def test_on_fail_retry_at_a_run_node_tries_again_after_a_wait_and_keeps_the_run_beating(factory: Harness) -> None:
    notebook = published(factory, board_fails=2)
    put_graph(factory, "default", graph_with({"kind": "assign", "on_fail": "retry"}))

    result = factory.run(LINK, "--tickets", "1,2")

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert notebook.assignees == {"1": [ACCOUNT], "2": [ACCOUNT]}
    failed = factory.times(run_id, "TrackerActionFailed")
    assert len(failed) == 2
    assert (failed[1] - failed[0]).total_seconds() >= 30
    assert [e["on_fail"] for e in factory.events(run_id, "TrackerActionFailed")] == ["retry", "retry"]
    assert factory.events(run_id, "Heartbeat")


def test_on_fail_escalate_at_a_run_node_takes_the_outcome_failed_to_escalate_to(factory: Harness) -> None:
    notebook = published(factory, board_fails=1)
    put_graph(factory, "default", graph_with({"kind": "assign", "on_fail": "escalate"}))

    result = factory.run(LINK, "--tickets", "1,2")

    run_id = run_id_of(result.out)
    assert walked(factory, run_id) == [("claim", "failed")]
    assert notebook.assignees == {}
    assert factory.events(run_id, "AttemptStarted") == []
    assert factory.events(run_id, "RunFinished")[0]["state"] == "partial"


def test_on_fail_warn_in_a_ticket_pipeline_goes_on_along_the_route_of_done(factory: Harness) -> None:
    notebook = published(factory, board_fails=1)
    factory.executor.ticket_scripts[("1", "implement")] = [FAILED_WORK]
    on_the_way_out(factory, {"kind": "label", "with": {"add": ["needs-human"]}})

    result = factory.run(LINK, "--tickets", "1")

    run_id = run_id_of(result.out)
    assert "factory: warning: #1: add:needs-human failed: the Tracker is down" in result.out
    assert notebook.labels == {}
    assert factory.events(run_id, "OperationFinished")[0]["outcome"] == "done"
    assert factory.events(run_id, "NodeEntered")[-1]["node"] == "abandoned"


def test_on_fail_retry_in_a_ticket_pipeline_tries_again_after_a_wait_while_the_run_waits_for_it(
    factory: Harness,
) -> None:
    notebook = published(factory, board_fails=1)
    factory.executor.ticket_scripts[("1", "implement")] = [FAILED_WORK]
    on_the_way_out(factory, {"kind": "label", "with": {"add": ["needs-human"]}, "on_fail": "retry"})

    result = factory.run(LINK, "--tickets", "1")

    run_id = run_id_of(result.out)
    assert notebook.labels == {"1": ["needs-human"]}
    [failed] = factory.times(run_id, "TrackerActionFailed")
    assert (factory.times(run_id, "TrackerActed")[0] - failed).total_seconds() >= 30
    assert factory.events(run_id, "NodeEntered")[-1]["node"] == "abandoned"


def test_a_ticket_cancelled_while_its_operation_waits_to_retry_lets_the_run_end(factory: Harness) -> None:
    published(factory, board_fails=1_000)
    factory.executor.ticket_scripts[("1", "implement")] = [FAILED_WORK]
    on_the_way_out(factory, {"kind": "label", "with": {"add": ["needs-human"]}, "on_fail": "retry"})

    owner = Owner(factory).then(lambda run_id: bool(factory.events(run_id, "TrackerActionFailed")), "cancel", "1")

    result = factory.run(LINK, "--tickets", "1")

    assert owner.results() == [(0, "Ticket 1 cancelled, abandoned", "")]
    assert factory.events(run_id_of(result.out), "RunFinished")[0]["state"] == "partial"


def test_a_ticket_cancelled_while_a_run_node_waits_to_retry_is_abandoned_before_it_starts(factory: Harness) -> None:
    notebook = published(factory, board_fails=1_000)
    put_graph(factory, "default", graph_with({"kind": "assign", "on_fail": "retry"}))
    owner = Owner(factory).then(lambda run_id: bool(factory.events(run_id, "TrackerActionFailed")), "cancel", "1")

    def tracker_back_once_cancelled(seconds: float) -> None:
        owner.on_sleep(seconds)
        if factory.events(factory.run_ids()[-1], "TicketCancelled"):
            notebook.board_fails = 0

    factory.clock.on_sleep = tracker_back_once_cancelled

    result = factory.run(LINK, "--tickets", "1")

    assert owner.results() == [(0, "Ticket 1 cancelled, abandoned", "")]
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "RunFinished")[0]["state"] == "partial"
    assert not factory.events(run_id, "AttemptStarted")


def test_on_fail_escalate_in_a_ticket_pipeline_goes_to_the_route_of_failed(factory: Harness) -> None:
    notebook = published(factory, board_fails=1)
    factory.pipeline["operations"]["tell"] = {
        "kind": "comment",
        "with": {"text": "Done."},
        "on_fail": "escalate",
        "next": {"done": "catch_up", "failed": "abandoned"},
    }
    factory.pipeline["stages"]["implement"]["next"]["done"] = "tell"

    result = factory.run(LINK, "--tickets", "1")

    run_id = run_id_of(result.out)
    assert notebook.comments == {}
    assert factory.events(run_id, "OperationFinished")[0]["outcome"] == "failed"
    assert factory.events(run_id, "NodeEntered")[-1]["node"] == "abandoned"


def without_board(factory: Harness) -> None:
    """Registers the Notebook of the link as a Tracker kind that serves no TicketBoard."""
    notebook = published(factory)

    def make(link: SpecLink, tools: Any) -> Tracker:
        tracker = NotebookTracker(notebook, link.tickets)
        return Tracker(tracker, tracker)

    factory.registries.trackers.register("notebook", Kind(make, picks=lambda spec: spec == LINK))


@pytest.mark.parametrize(
    ("use", "node"),
    [
        (lambda factory: put_graph(factory, "default", graph_with({"kind": "assign"})), "claim"),
        (lambda factory: on_the_way_out(factory, {"kind": "label", "with": {"add": ["needs-human"]}}), "tell"),
    ],
    ids=["run_graph", "ticket_pipeline"],
)
def test_a_tracker_operation_cannot_start_on_a_tracker_without_a_ticket_board(
    factory: Harness, use: Callable[[Harness], None], node: str
) -> None:
    without_board(factory)
    use(factory)

    result = factory.run(LINK, "--tickets", "1")

    assert result.code == 2
    assert f"the Operation {node} needs the role TicketBoard, which the Tracker of {LINK} does not serve" in result.err
    assert factory.cli("check", LINK, "--project", str(factory.project_file)).code == 2


@pytest.mark.parametrize(
    ("node", "error"),
    [
        pytest.param({"kind": "label"}, "nodes.claim.with: names no label to add or remove", id="label-without-labels"),
        pytest.param(
            {"kind": "label", "with": {"add": "needs-human"}},
            "nodes.claim.with.add: must be a list of names",
            id="labels-not-a-list",
        ),
        pytest.param({"kind": "comment"}, "nodes.claim.with.text: must be a text", id="comment-without-text"),
        pytest.param(
            {"kind": "comment", "with": {"text": "On ${nope}."}},
            "nodes.claim.with.text: unknown reference ${nope}",
            id="unknown-reference",
        ),
        pytest.param(
            {"kind": "comment", "with": {"text": "On ${ticket}.", "target": "spec"}},
            "nodes.claim.with.text: a comment on the Spec has no reference ${ticket}",
            id="ticket-reference-on-the-spec",
        ),
        pytest.param(
            {"kind": "assign", "with": {"target": "pr"}}, "nodes.claim.with.target: must be spec", id="unknown-target"
        ),
        pytest.param(
            {"kind": "assign", "on_fail": "ignore"},
            "nodes.claim.on_fail: must be one of warn, retry, escalate",
            id="unknown-on-fail",
        ),
        pytest.param(
            {"kind": "assign", "next": {"done": "build", "failed": "ended"}},
            "nodes.claim: there is no outcome failed",
            id="failed-without-escalate",
        ),
        pytest.param(
            {"kind": "assign", "with": {"add": ["x"]}},
            "nodes.claim.with.add: unknown field",
            id="field-of-another-kind",
        ),
    ],
)
def test_check_rejects_what_a_tracker_operation_says_at_a_run_node(
    factory: Harness, node: dict[str, Any], error: str
) -> None:
    path = put_graph(factory, "default", graph_with(node))

    result = factory.check()

    assert result.code == 2
    assert f"factory: {display_path(path)}: {error}" in result.err.splitlines()


@pytest.mark.parametrize(
    ("node", "error"),
    [
        pytest.param(
            {"kind": "label", "with": {"add": ["x"], "target": "spec"}, "next": {"done": "abandoned"}},
            "operations.tell.with.target: unknown field",
            id="target-in-a-pipeline",
        ),
        pytest.param(
            {"kind": "merge", "on_fail": "warn", "next": {"merged": "done"}},
            "operations.tell.on_fail: only a Tracker Operation has on_fail",
            id="on-fail-of-another-kind",
        ),
    ],
)
def test_check_rejects_what_a_tracker_operation_says_in_a_pipeline(
    factory: Harness, node: dict[str, Any], error: str
) -> None:
    factory.pipeline["operations"]["tell"] = node
    factory.pipeline["escalate_to"] = "tell"

    result = factory.check()

    assert result.code == 2
    assert error in result.err


def test_check_takes_on_fail_escalate_without_a_route_of_failed_where_the_pipeline_has_escalate_to(
    factory: Harness,
) -> None:
    factory.pipeline["operations"]["tell"] = {"kind": "assign", "on_fail": "escalate", "next": {"done": "abandoned"}}
    factory.pipeline["stages"]["implement"]["next"]["failed"] = "tell"

    result = factory.check()

    assert result.code == 0, result.err
