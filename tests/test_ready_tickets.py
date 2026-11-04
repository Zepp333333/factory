"""Scenarios of the ready label: a Run takes only the Tickets that carry it, whatever the kind of its Tracker."""

from pathlib import Path

import pytest

from factory.config import Sources
from harness import READY, AgentScript, Harness, run_id_of, ticket_text
from test_recovery import killed_runs_run_no_handler  # noqa: F401
from test_settings import crashed, pair
from test_tracker_run import Tracker, tracker  # noqa: F401

OUTPUT = {"summary": "s", "handoff": "h"}


def mixed(factory: Harness, labels: dict[str, list[str]] | None = None, blocked: bool = False) -> Path:
    """Writes a Spec folder of Tickets `go` and `wait`, `go` blocked by `wait` when `blocked`, with `wait` unready."""
    factory.executor.scripts["implement"] = AgentScript(run=["echo line >> log.txt"], output=OUTPUT)
    return factory.write_spec(
        "Mixed",
        "Two.",
        {"go": ticket_text("Go", ["wait"] if blocked else None), "wait": ticket_text("Wait")},
        labels=labels or {"wait": ["needs-triage", "bug"]},
    )


def test_a_run_takes_only_the_ready_tickets_and_lists_the_rest_with_their_labels(factory: Harness) -> None:
    result = factory.run(mixed(factory))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [event["ticket"] for event in factory.events(run_id, "TicketClaimed")] == ["go"]
    assert "factory: leaves out wait Wait, which lacks ready-for-agent and carries needs-triage, bug\n" in result.err
    [snapshotted] = factory.events(run_id, "SpecSnapshotted")
    assert [t["id"] for t in snapshotted["snapshot"]["tickets"]] == ["go"]
    [(left_id, left_labels)] = [(t["id"], t["labels"]) for t in snapshotted["snapshot"]["left_out"]]
    assert (left_id, left_labels) == ("wait", ["needs-triage", "bug"])


def test_a_ticket_without_labels_is_listed_as_carrying_none(factory: Harness) -> None:
    result = factory.run(mixed(factory, {"wait": []}))

    assert result.code == 0, result.out + result.err
    assert "factory: leaves out wait Wait, which lacks ready-for-agent and carries no label\n" in result.err


def test_a_run_that_takes_every_ticket_lists_none(factory: Harness) -> None:
    result = factory.run(pair(factory))

    assert result.code == 0, result.out + result.err
    assert "leaves out" not in result.err
    assert factory.events(run_id_of(result.out), "SpecSnapshotted")[0]["snapshot"]["left_out"] == []


def test_a_spec_with_no_ready_ticket_stops_the_run_and_names_the_label(factory: Harness) -> None:
    spec = mixed(factory, {"go": ["needs-info"], "wait": []})
    before = factory.branches()

    result = factory.run(spec)

    assert result.code == 2
    assert result.err == f"factory: {spec} has no open Ticket with the label {READY}\n"
    assert factory.branches() == before


def test_tickets_flag_with_an_unready_ticket_stops_the_run_and_names_it(factory: Harness) -> None:
    spec = mixed(factory)
    before = factory.branches()

    result = factory.run(spec, "--tickets", "go,wait")

    assert result.code == 2
    assert result.err == f"factory: wait lacks the label {READY}, so this Run cannot take it\n"
    assert factory.branches() == before


def test_tickets_flag_lists_nothing_as_the_owner_chose_the_tickets(factory: Harness) -> None:
    result = factory.run(mixed(factory), "--tickets", "go")

    assert result.code == 0, result.out + result.err
    assert "leaves out" not in result.err


def test_an_unready_open_blocker_stops_the_run_and_names_its_missing_label(factory: Harness) -> None:
    spec = mixed(factory, blocked=True)
    before = factory.branches()

    result = factory.run(spec)

    assert result.code == 2
    assert result.err == (
        f"factory: go is blocked by open wait, which this Run does not take and which lacks the label {READY}\n"
    )
    assert factory.branches() == before


def test_tickets_flag_with_an_unready_blocker_it_does_not_take_names_the_missing_label(factory: Harness) -> None:
    result = factory.run(mixed(factory, blocked=True), "--tickets", "go")

    assert result.code == 2
    assert result.err == (
        f"factory: go is blocked by open wait, which this Run does not take and which lacks the label {READY}\n"
    )


def test_an_unready_ticket_blocked_from_outside_the_spec_stays_out_and_stops_nothing(tracker: Tracker) -> None:  # noqa: F811
    factory = tracker.factory
    factory.executor.scripts["implement"] = AgentScript(run=["echo line >> log.txt"], output=OUTPUT)
    tracker.publish()
    if tracker.name == "github":
        factory.gh.issue(40, "Elsewhere", repo="owner/other")
        factory.gh.issue(29, "Later", labels=["needs-info"], blocked_by=["owner/other#40"])
        url = factory.gh.issue(21, "Greeting", "Say hello.", sub_issues=[28, 29])
    else:
        factory.glab.set(linked_items=True)
        factory.glab.issue(40, "Elsewhere", project="other/project")
        factory.glab.issue(29, "Later", labels=["needs-info"], blocked_by=["other/project#40"])
        url = factory.glab.issue(21, "Greeting", "Say hello.", kind="Issue", children=[28, 29])

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    assert "factory: leaves out #29 Later, which lacks ready-for-agent and carries needs-info\n" in result.err
    run_id = run_id_of(result.out)
    assert [event["ticket"] for event in factory.events(run_id, "TicketClaimed")] == ["28"]


def test_a_ready_ticket_blocked_by_an_unready_issue_outside_the_spec_names_the_missing_label(
    tracker: Tracker,  # noqa: F811
) -> None:
    factory = tracker.factory
    tracker.publish()
    tracker.ticket(40, "Elsewhere", labels=["needs-info"])
    if tracker.name == "github":
        factory.gh.issue(29, "Later", blocked_by=[40])
        url = factory.gh.issue(21, "Greeting", "Say hello.", sub_issues=[29])
    else:
        factory.glab.issue(29, "Later", "---\nblocked_by: [40]\n---\nLater.")
        url = factory.glab.issue(21, "Greeting", "Say hello.", kind="Issue", children=[29])

    result = factory.run(url)

    assert result.code == 2
    assert result.err == (
        f"factory: #29 is blocked by open #40, which this Run does not take and which lacks the label {READY}\n"
    )


def test_a_closed_unready_blocker_counts_as_done(tracker: Tracker) -> None:  # noqa: F811
    factory = tracker.factory
    factory.executor.scripts["implement"] = AgentScript(run=["echo line >> log.txt"], output=OUTPUT)
    tracker.publish()
    tracker.ticket(40, "Done", state="closed", labels=[])
    if tracker.name == "github":
        factory.gh.issue(29, "Later", blocked_by=[40])
        url = factory.gh.issue(21, "Greeting", "Say hello.", sub_issues=[29, 40])
    else:
        factory.glab.issue(29, "Later", "---\nblocked_by: [40]\n---\nLater.")
        url = factory.glab.issue(21, "Greeting", "Say hello.", kind="Issue", children=[29, 40])

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    assert [event["ticket"] for event in factory.events(run_id_of(result.out), "TicketClaimed")] == ["29"]


def test_the_project_file_and_set_change_the_label_and_the_run_freezes_it(factory: Harness) -> None:
    spec = mixed(factory, {"go": ["cooked"], "wait": ["fresh"]})
    factory.project["tracker"] = {"ready_label": "cooked"}

    by_file = factory.run(spec)

    assert by_file.code == 0, by_file.out + by_file.err
    run_id = run_id_of(by_file.out)
    assert [event["ticket"] for event in factory.events(run_id, "TicketClaimed")] == ["go"]
    assert factory.events(run_id, "RunStarted")[0]["settings"]["tracker"] == {"ready_label": "cooked"}

    refused = factory.run(spec, "--set", "tracker.ready_label=plated")

    assert refused.code == 2
    assert refused.err == f"factory: {spec} has no open Ticket with the label plated\n"


def test_set_takes_tickets_by_the_label_it_names_over_the_project_file(factory: Harness) -> None:
    spec = mixed(factory, {"go": ["cooked"], "wait": ["fresh"]})
    factory.project["tracker"] = {"ready_label": "cooked"}

    result = factory.run(spec, "--set", "tracker.ready_label=fresh")

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [event["ticket"] for event in factory.events(run_id, "TicketClaimed")] == ["wait"]
    assert "Settings unlike the project file: tracker=" in factory.cli("status", run_id).out


def test_a_resumed_run_keeps_the_label_and_the_tickets_it_started_with(factory: Harness) -> None:
    factory.executor.scripts["implement"] = AgentScript(run=["echo line >> log.txt"], output=OUTPUT)
    spec = factory.write_spec(
        "Mixed",
        "Two.",
        {"t1": ticket_text("One"), "t2": ticket_text("Two")},
        labels={"t1": ["cooked"], "t2": ["fresh"]},
    )
    run_id = crashed(factory, spec, "--set", "tracker.ready_label=cooked")
    factory.project["tracker"] = {"ready_label": "fresh"}

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert {event["ticket"] for event in factory.events(run_id, "TicketClaimed")} == {"t1"}
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"
    [started] = factory.events(run_id, "RunStarted")
    assert started["settings"]["tracker"] == {"ready_label": "cooked"}


def test_the_recorded_label_wins_over_the_project_file() -> None:
    sources = Sources(recorded={"tracker": {"ready_label": "cooked"}})

    assert sources.resolve({"tracker": {"ready_label": "fresh"}}, {})["tracker"] == {"ready_label": "cooked"}


@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("tracker.ready_label=''", "tracker.ready_label: must be a label"),
        ("tracker.ready_label=12", "tracker.ready_label: must be a label"),
        ("tracker.colour=red", "tracker.colour: unknown field"),
        ("tracker=none", "tracker: must be a mapping"),
    ],
    ids=["empty", "number", "unknown", "no-mapping"],
)
def test_a_bad_tracker_block_stops_the_run_before_it_starts(factory: Harness, setting: str, message: str) -> None:
    result = factory.run(pair(factory), "--set", setting)

    assert result.code == 2
    assert message in result.err
