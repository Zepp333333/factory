"""The Run node `select` produces the Tickets a Run takes, their order and their dependencies (ADR 0014)."""

import json
from pathlib import Path
from typing import Any

import pytest

from factory.core.events import Event
from factory.journal.journal import Journal
from factory.ports.tracker import SpecSnapshot, TicketSnapshot
from fake_kinds import Notebook
from harness import AgentScript, Harness, run_id_of, ticket_text
from test_recovery import (
    STALE,
    Crash,
    attempt_started,
    crashed_run,
    killed_runs_run_no_handler,  # noqa: F401
)
from test_run_graph import HELLO, ONLY_TICKETS, entered, put_graph, walked

READY = "ready-for-agent"
LINK = "https://tracker.example/spec/1"


def selecting(factory: Harness, kind: str = "filter", **with_: Any) -> None:
    """Writes a Run graph that selects with `kind` and `with_`, builds and opens the PR."""
    select = {"kind": kind, **({"with": with_} if with_ else {}), "next": {"selected": "build", "empty": "ended"}}
    build = {
        "kind": "tickets",
        "with": {"pipeline": "${vars.pipeline}"},
        "next": {"all_merged": "open_pr", "some_merged": "open_pr", "none_merged": "ended"},
    }
    open_pr = {"kind": "open_pr", "next": {"opened": "ended", "failed": "ended"}}
    put_graph(factory, "default", {"start": "select", "nodes": {"select": select, "build": build, "open_pr": open_pr}})


def spec_of(factory: Harness, tickets: dict[str, str], labels: dict[str, list[str]]) -> Path:
    factory.executor.scripts["implement"] = HELLO
    return factory.write_spec("Greeting", "Say hello.", tickets, labels)


def selected(factory: Harness, run_id: str) -> dict[str, Any]:
    [event] = factory.events(run_id, "TicketsSelected")
    return event


def test_the_default_run_graph_takes_every_ticket_it_read_in_their_order_with_their_blockers(factory: Harness) -> None:
    factory.executor.scripts["implement"] = [HELLO, AgentScript(files={"bye.txt": "bye\n"}, output=HELLO.output)]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("Greet"), "t2": ticket_text("Bye", ["t1"])})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert entered(factory, run_id) == ["select", "build", "open_pr", "ci"]
    assert walked(factory, run_id)[0] == ("select", "selected")
    event = selected(factory, run_id)
    assert event["tickets"] == [{"id": "t1", "blocked_by": []}, {"id": "t2", "blocked_by": ["t1"]}]
    assert event["left_out"] == []
    assert "leaves out" not in result.err + result.out


def test_a_filter_takes_only_the_labelled_tickets_and_one_that_depends_on_one_it_did_not_take_does_not_start(
    factory: Harness,
) -> None:
    selecting(factory, label=READY, without=["hold"])
    tickets = {
        "t1": ticket_text("Lay"),
        "t2": ticket_text("Blocked", ["t1"]),
        "t3": ticket_text("Greet"),
        "t4": ticket_text("Behind it", ["t2"]),
        "t5": ticket_text("Held"),
    }
    labels = {"t2": [READY], "t3": [READY, "bug"], "t4": [READY], "t5": [READY, "hold"]}
    spec = spec_of(factory, tickets, labels)

    result = factory.run(spec)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[:2] == [("select", "selected"), ("build", "some_merged")]
    assert [e["ticket"] for e in factory.events(run_id, "TicketClaimed")] == ["t3"]
    event = selected(factory, run_id)
    assert event["with"] == {"label": [READY], "without": ["hold"]}
    assert json.loads(Path(event["artifact"]).read_text()) == {
        "tickets": [
            {"id": "t2", "blocked_by": ["t1"]},
            {"id": "t3", "blocked_by": []},
            {"id": "t4", "blocked_by": ["t2"]},
        ],
        "left_out": ["t1", "t5"],
    }
    assert "factory: warning: leaves out t1 Lay, which carries no label" in result.err + result.out
    rows = factory.cli("status", run_id).out.splitlines()
    assert not any(line.startswith(("t1 ", "t5 ")) for line in rows)
    assert "Ticket t2 never starts, blocked by t1, which the Run did not take" in rows
    assert "Ticket t4 never starts, blocked by t1, which the Run did not take" in rows
    [opened] = factory.events(run_id, "PullRequestRecorded")
    body = Path(opened["path"]).read_text()
    assert "t2 Blocked: not started, blocked by t1" in body
    assert "t1 Lay" not in body


@pytest.mark.parametrize(
    ("label", "without", "taken"),
    [
        ([READY, "small"], ["hold"], ["both"]),
        ([READY], [], ["both", "one", "held"]),
        ([], ["hold"], ["both", "one", "none"]),
        ([], [], ["both", "one", "held", "none"]),
    ],
    ids=["every-label-and-none-of-without", "only-label", "only-without", "neither"],
)
def test_a_snapshot_keeps_the_tickets_that_carry_every_label_and_none_of_without(
    label: list[str], without: list[str], taken: list[str]
) -> None:
    carried = {"both": [READY, "small"], "one": [READY], "held": [READY, "small", "hold"], "none": []}
    tickets = [TicketSnapshot(name, name, "Text.", labels=labels) for name, labels in carried.items()]
    snapshot = SpecSnapshot(LINK, "Greeting", "Say hello.", tickets)

    assert [t.id for t in snapshot.carrying(label, without)] == taken


def test_overrides_make_the_select_node_of_the_default_run_graph_a_filter(factory: Harness) -> None:
    factory.project["overrides"] = {"nodes": {"select": {"kind": "filter", "with": {"label": READY}}}}
    spec = spec_of(factory, {"t1": ticket_text("Greet"), "t2": ticket_text("Bye")}, {"t1": [READY]})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert [t["id"] for t in selected(factory, run_id_of(result.out))["tickets"]] == ["t1"]


def test_a_spec_with_no_matching_ticket_ends_the_run_through_empty_before_any_workspace_is_made(
    factory: Harness,
) -> None:
    selecting(factory, label=READY)
    spec = spec_of(factory, {"t1": ticket_text("Greet")}, {"t1": ["bug"]})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id) == [("select", "empty")]
    assert factory.events(run_id, "TicketClaimed") == []
    assert factory.events(run_id, "AttemptStarted") == []
    assert factory.events(run_id, "PullRequestRecorded") == []
    assert not factory.workspace(run_id, "t1").exists()
    assert f"Run {run_id} finished: done" in result.out


def test_a_tracker_operation_after_the_selection_acts_only_on_the_tickets_it_took(factory: Harness) -> None:
    notebook = Notebook(
        LINK,
        "Greeting",
        "Say hello.",
        [TicketSnapshot("1", "Greet", "Write."), TicketSnapshot("2", "Bye", "Write.")],
        labels={"1": [READY], "2": []},
    )
    factory.registries.trackers.register("notebook", notebook.kind())
    factory.executor.scripts["implement"] = HELLO
    build = dict(ONLY_TICKETS["nodes"]["build"])
    graph = {
        "start": "select",
        "nodes": {
            "select": {"kind": "filter", "with": {"label": READY}, "next": {"selected": "claim", "empty": "ended"}},
            "claim": {"kind": "label", "with": {"add": ["in-progress"]}, "next": {"done": "build"}},
            "build": build,
        },
    }
    put_graph(factory, "default", graph)

    result = factory.run(LINK)

    assert result.code == 0, result.out + result.err
    assert notebook.labels == {"1": [READY, "in-progress"], "2": []}


def test_a_run_killed_after_it_selected_resumes_with_the_tickets_it_took(factory: Harness) -> None:
    selecting(factory, label=READY)
    stuck = AgentScript(files={"junk.txt": "half\n"}, output=HELLO.output, wait_for=factory.root / "go")
    factory.executor.scripts["implement"] = [stuck, HELLO]
    spec = factory.write_spec(
        "Greeting", "Say hello.", {"t1": ticket_text("Greet"), "t2": ticket_text("Bye")}, {"t1": [READY]}
    )
    run_id = crashed_run(factory, spec, attempt_started(factory))
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert len(factory.events(run_id, "TicketsSelected")) == 1
    assert [e["ticket"] for e in factory.events(run_id, "TicketClaimed")] == ["t1"]
    assert not any(line.startswith("t2 ") for line in factory.cli("status", run_id).out.splitlines())


@pytest.mark.parametrize("journaled", [False, True], ids=["before-its-event", "after-its-event"])
def test_a_run_killed_in_the_select_node_ends_it_on_resume_with_one_selection(
    factory: Harness, monkeypatch: pytest.MonkeyPatch, journaled: bool
) -> None:
    selecting(factory, label=READY)
    factory.executor.scripts["implement"] = HELLO
    spec = factory.write_spec(
        "Greeting", "Say hello.", {"t1": ticket_text("Greet"), "t2": ticket_text("Bye")}, {"t1": [READY]}
    )
    append = Journal.append

    def killed(self: Journal, run_id: str, type: Event, payload: dict[str, Any]) -> Any:
        if type != Event.TicketsSelected:
            return append(self, run_id, type, payload)
        monkeypatch.setattr(Journal, "append", append)
        if journaled:
            append(self, run_id, type, payload)
        raise Crash

    monkeypatch.setattr(Journal, "append", killed)
    run_id = crashed_run(factory, spec, lambda _: False)
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert entered(factory, run_id)[0:2] == ["select", "build"]
    assert walked(factory, run_id)[0] == ("select", "selected")
    assert [t["id"] for t in selected(factory, run_id)["tickets"]] == ["t1"]


def selecting_with(body: dict[str, Any]) -> dict[str, Any]:
    routes = {"selected": "build", "empty": "ended"}
    return {"start": "select", "nodes": {"select": {**body, "next": routes}, "build": ONLY_TICKETS["nodes"]["build"]}}


BUILD_THEN_SELECT = {
    "start": "build",
    "nodes": {
        "build": {
            **ONLY_TICKETS["nodes"]["build"],
            "next": {"all_merged": "select", "some_merged": "ended", "none_merged": "ended"},
        },
        "select": {"kind": "all", "next": {"selected": "ended", "empty": "ended"}},
    },
}
SELECTING_TWICE = {
    "start": "first",
    "nodes": {
        "first": {"kind": "all", "next": {"selected": "second", "empty": "ended"}},
        "second": {"kind": "filter", "with": {"label": READY}, "next": {"selected": "build", "empty": "ended"}},
        **ONLY_TICKETS["nodes"],
    },
}
NO_ROUTE_FOR_EMPTY = {
    "start": "select",
    "nodes": {"select": {"kind": "all", "next": {"selected": "build"}}, **ONLY_TICKETS["nodes"]},
}


@pytest.mark.parametrize(
    ("graph", "error"),
    [
        (selecting_with({"kind": "filter"}), "nodes.select.with: names no label to take or leave out"),
        (selecting_with({"kind": "filter", "with": {"label": 3}}), "nodes.select.with.label: must be a list of names"),
        (
            selecting_with({"kind": "filter", "with": {"without": [""]}}),
            "nodes.select.with.without: must be a list of names",
        ),
        (selecting_with({"kind": "filter", "with": {"labels": ["a"]}}), "nodes.select.with.labels: unknown field"),
        (selecting_with({"kind": "all", "with": {"label": "a"}}), "nodes.select.with.label: unknown field"),
        (NO_ROUTE_FOR_EMPTY, "empty"),
        (BUILD_THEN_SELECT, "nodes.select: a path reaches this all node after the tickets node"),
        (SELECTING_TWICE, "nodes: a Run graph selects its Tickets once, and it has first, second"),
    ],
    ids=[
        "filter-naming-no-label",
        "label-not-a-name",
        "without-an-empty-name",
        "unknown-field",
        "all-with-a-with",
        "no-route-for-empty",
        "after-tickets",
        "selecting-twice",
    ],
)
def test_check_refuses_a_bad_select_node(factory: Harness, graph: dict[str, Any], error: str) -> None:
    put_graph(factory, "default", graph)

    result = factory.check()

    assert result.code != 0
    assert error in result.err


def test_a_filter_on_github_leaves_a_ticket_blocked_in_another_repository_unstarted_and_does_not_stop_the_run(
    factory: Harness,
) -> None:
    factory.executor.scripts["implement"] = HELLO
    selecting(factory, label=READY)
    factory.gh.issue(5, "Elsewhere", repo="owner/other")
    factory.gh.issue(28, "Greet", labels=[READY])
    factory.gh.issue(29, "Stuck", blocked_by=["owner/other#5"], labels=[READY])
    factory.gh.issue(30, "Unlabelled, and stuck too", blocked_by=["owner/other#5"])
    factory.gh.issue(31, "Waits for what stays out", blocked_by=[30], labels=[READY])
    url = factory.gh.issue(21, "Greeting", sub_issues=[28, 29, 30, 31])

    result = factory.run(url)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    assert [e["ticket"] for e in factory.events(run_id, "TicketClaimed")] == ["28"]
    assert selected(factory, run_id)["left_out"] == ["30"]
    status = factory.cli("status", run_id).out
    assert "never starts, blocked by owner/other#5, which the Run did not take" in status
    assert "never starts, blocked by #30, which the Run did not take" in status
    [pull] = factory.gh.pulls()
    assert "#29 Stuck: not started, blocked by owner/other#5" in pull["body"]
    assert "#31 Waits for what stays out: not started, blocked by #30" in pull["body"]
