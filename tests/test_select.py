"""The Run node `select` produces the Tickets a Run takes, their order and their dependencies (ADR 0014)."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from factory.core.events import Event
from factory.journal.journal import Journal
from factory.ports.tracker import SpecSnapshot, TicketSnapshot
from fake_kinds import Notebook
from harness import AgentScript, Harness, run_id_of, ticket_text
from test_describer import APPEND, DESCRIPTION
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


def planning(factory: Harness, **fields: Any) -> None:
    """Writes a Run graph whose node `select` is a Stage of the Role `planner`, which builds and opens the PR."""
    select = {"role": "planner", **fields, "next": {"selected": "build", "empty": "ended"}}
    build = {
        "kind": "tickets",
        "with": {"pipeline": "${vars.pipeline}"},
        "next": {"all_merged": "open_pr", "some_merged": "open_pr", "none_merged": "ended"},
    }
    open_pr = {"kind": "open_pr", "next": {"opened": "ended", "failed": "ended"}}
    graph = {"start": "select", "escalate_to": "ended", "nodes": {"select": select, "build": build, "open_pr": open_pr}}
    put_graph(factory, "default", graph)


def plan(tickets: list[str], left_out: dict[str, str] | None = None, **fields: Any) -> AgentScript:
    reasons = [{"ticket": id, "reason": reason} for id, reason in (left_out or {}).items()]
    return AgentScript(output={"tickets": tickets, "left_out": reasons}, **fields)


THREE = {"t1": ticket_text("First"), "t2": ticket_text("Second"), "t3": ticket_text("Third")}
REVERSED = plan(["t3", "t1"], {"t2": "It waits for the next release."})


def test_a_planner_that_takes_two_of_three_tickets_in_reverse_order_makes_the_run_start_them_in_that_order(
    factory: Harness,
) -> None:
    planning(factory)
    factory.executor.scripts["implement"] = [
        AgentScript(files={"third.txt": "third\n"}, output=HELLO.output),
        AgentScript(files={"first.txt": "first\n"}, output=HELLO.output),
    ]
    factory.executor.scripts["select"] = REVERSED
    tickets = {**THREE, "t2": ticket_text("Second", ["t1"])}
    spec = factory.write_spec("Greeting", "Say hello.", tickets, {"t3": [READY]})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [e["ticket"] for e in factory.events(run_id, "TicketClaimed")] == ["t3", "t1"]
    assert walked(factory, run_id)[:2] == [("select", "selected"), ("build", "all_merged")]
    event = selected(factory, run_id)
    assert event["role"] == "planner"
    assert json.loads(Path(event["artifact"]).read_text()) == {
        "tickets": [{"id": "t3", "blocked_by": []}, {"id": "t1", "blocked_by": []}],
        "left_out": ["t2"],
        "reasons": {"t2": "It waits for the next release."},
    }
    output = Path(event["output"])
    assert json.loads(output.read_text())["tickets"] == ["t3", "t1"]
    [started] = factory.events(run_id, "PlannerStarted")
    run_dir = factory.run_dir(run_id)
    assert started["brief"]["inputs"] == [
        {"source": "spec", "path": str(run_dir / "spec.md")},
        *(
            {
                "source": "ticket",
                "ticket": id,
                "path": str(run_dir / "tickets" / id / "ticket.md"),
                "title": title,
                "blocked_by": blocked_by,
                "labels": labels,
            }
            for id, title, blocked_by, labels in (
                ("t1", "First", [], []),
                ("t2", "Second", ["t1"], []),
                ("t3", "Third", [], [READY]),
            )
        ),
    ]
    assert "factory: warning: leaves out t2 Second: It waits for the next release." in result.err + result.out
    assert factory.slots() == []
    assert f"factory/planner-{run_id}" not in factory.branches()
    assert not factory.sbx.state()["sandboxes"]
    [run] = json.loads(factory.cli("stats", run_id, "--json").out)["runs"]
    assert [(a["node"], a["attempt"]) for a in run["attempts"] if a["ticket"] == "/planner"] == [("select", 1)]


def test_a_planner_output_with_a_foreign_id_fails_the_attempt_with_a_reason_the_next_attempt_reads(
    factory: Harness,
) -> None:
    planning(factory, attempts=2)
    factory.executor.scripts["implement"] = HELLO
    factory.executor.scripts["select"] = [plan(["t1", "t9"]), plan(["t1"])]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("First")})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [failed] = factory.events(run_id, "PlannerFailed")
    assert (failed["cause"], failed["attempt"]) == ("invalid_output", 1)
    assert "t9 is no Ticket of the Spec" in failed["reason"]
    first, second = factory.events(run_id, "PlannerStarted")
    assert not any(entry["source"] == "cause" for entry in first["brief"]["inputs"])
    assert {"source": "cause", "attempt": 1, "reason": failed["reason"]} in second["brief"]["inputs"]
    assert [e["ticket"] for e in factory.events(run_id, "TicketClaimed")] == ["t1"]
    assert factory.slots() == []


@pytest.mark.parametrize(
    ("taken", "left_out", "problem"),
    [
        (["t1", "t9"], ["t2", "t3"], "t9 is no Ticket of the Spec"),
        (["t1", "t2", "t3"], ["t9"], "t9 is no Ticket of the Spec"),
        (["t1", "t1", "t2"], ["t3"], "t1 is named twice"),
        (["t1", "t2"], ["t2", "t3"], "t2 is named twice"),
        (["t3", "t1"], ["t2"], "t3 comes before t1, which it depends on"),
        (["t1"], ["t2"], "t3 is neither taken nor left out with a reason"),
        (["t1", "t3"], ["t2"], None),
        ([], ["t1", "t2", "t3"], None),
        (["t3"], ["t1", "t2"], None),
    ],
    ids=[
        "foreign-taken",
        "foreign-left-out",
        "repeated",
        "taken-and-left-out",
        "before-what-it-depends-on",
        "not-named",
        "in-order",
        "none-taken",
        "blocker-left-out",
    ],
)
def test_a_snapshot_refuses_a_plan_that_names_a_foreign_ticket_repeats_one_or_breaks_the_order_of_its_blockers(
    taken: list[str], left_out: list[str], problem: str | None
) -> None:
    tickets = [
        TicketSnapshot("t1", "1", "T."),
        TicketSnapshot("t2", "2", "T."),
        TicketSnapshot("t3", "3", "T.", ["t1"]),
    ]
    snapshot = SpecSnapshot(LINK, "Greeting", "Say hello.", tickets)

    assert snapshot.plan_problems(taken, left_out) == ([problem] if problem else [])


@pytest.mark.parametrize(
    ("scripts", "causes"),
    [
        ([plan(["t9"]), plan(["t1", "t1"])], ["invalid_output", "invalid_output"]),
        ([AgentScript(output={"tickets": "t1"}), AgentScript(fail="crash")], ["invalid_output", "executor_crash"]),
    ],
    ids=["refused-output", "broken-schema-and-crash"],
)
def test_a_planner_that_fails_every_attempt_ends_the_node_failed_and_starts_no_ticket(
    factory: Harness, scripts: list[AgentScript], causes: list[str]
) -> None:
    planning(factory, attempts=2)
    factory.executor.scripts["select"] = scripts
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("First")})

    result = factory.run(spec)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id) == [("select", "failed")]
    assert [e["cause"] for e in factory.events(run_id, "PlannerFailed")] == causes
    assert factory.events(run_id, "TicketsSelected") == []
    assert factory.events(run_id, "TicketClaimed") == []
    assert factory.events(run_id, "PullRequestRecorded") == []
    assert factory.slots() == []


def test_a_planner_that_takes_no_ticket_ends_the_run_through_empty(factory: Harness) -> None:
    planning(factory)
    factory.executor.scripts["select"] = plan([], {"t1": "Nothing is ready."})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("First")})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id) == [("select", "empty")]
    assert factory.events(run_id, "TicketClaimed") == []


def test_a_sandbox_that_fails_a_planner_spends_no_attempt_up_to_sandbox_retries(factory: Harness) -> None:
    planning(factory)
    factory.executor.scripts["implement"] = HELLO
    factory.executor.scripts["select"] = plan(["t1"])
    factory.sandbox.refuse = 2
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("First")})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [e["cause"] for e in factory.events(run_id, "PlannerFailed")] == ["sandbox_start", "sandbox_start"]
    assert [e["attempt"] for e in factory.events(run_id, "PlannerStarted")] == [1, 2, 3]
    assert not any(e["source"] == "cause" for e in factory.events(run_id, "PlannerStarted")[2]["brief"]["inputs"])


def test_a_resumed_run_that_selected_does_not_start_the_planner_again(factory: Harness) -> None:
    planning(factory)
    stuck = AgentScript(files={"junk.txt": "half\n"}, output=HELLO.output, wait_for=factory.root / "go")
    factory.executor.scripts["implement"] = [stuck, HELLO]
    factory.executor.scripts["select"] = plan(["t1"], {"t2": "Later."})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("First"), "t2": ticket_text("Second")})
    run_id = crashed_run(factory, spec, attempt_started(factory))
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert len(factory.events(run_id, "PlannerStarted")) == 1
    assert len(factory.events(run_id, "TicketsSelected")) == 1
    assert [e["ticket"] for e in factory.events(run_id, "TicketClaimed")] == ["t1"]


def test_a_run_killed_between_the_planners_output_and_the_selection_does_not_ask_it_again(
    factory: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    planning(factory)
    factory.executor.scripts["implement"] = HELLO
    factory.executor.scripts["select"] = plan(["t1"], {"t2": "Later."})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("First"), "t2": ticket_text("Second")})
    append = Journal.append

    def killed(self: Journal, run_id: str, type: Event, payload: dict[str, Any]) -> Any:
        if type != Event.TicketsSelected:
            return append(self, run_id, type, payload)
        monkeypatch.setattr(Journal, "append", append)
        raise Crash

    monkeypatch.setattr(Journal, "append", killed)
    run_id = crashed_run(factory, spec, lambda _: False)
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert len(factory.events(run_id, "PlannerStarted")) == 1
    assert [t["id"] for t in selected(factory, run_id)["tickets"]] == ["t1"]


def test_a_run_killed_while_the_planner_works_loses_that_attempt_and_starts_a_new_one_on_resume(
    factory: Harness,
) -> None:
    planning(factory)
    factory.executor.scripts["implement"] = HELLO
    stuck = plan(["t1"], files={"junk.txt": "half\n"}, wait_for=factory.root / "go")
    factory.executor.scripts["select"] = [stuck, plan(["t1"])]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("First")})
    run_id = crashed_run(factory, spec, lambda run_id: bool(factory.events(run_id, "PlannerStarted")))
    factory.clock.sleep(STALE.seconds)

    assert "Run node: select" in factory.cli("status", run_id).out.splitlines()
    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert [e["attempt"] for e in factory.events(run_id, "PlannerStarted")] == [1, 2]
    assert factory.events(run_id, "PlannerFailed") == []
    assert walked(factory, run_id)[0] == ("select", "selected")
    assert f"factory/planner-{run_id}" not in factory.branches()
    assert "+half" in (factory.run_dir(run_id) / "planner" / "attempt-1" / "discarded.patch").read_text()


def test_the_library_ships_a_run_graph_whose_planner_selects_and_whose_describer_writes_the_pr_text(
    factory: Harness,
) -> None:
    factory.project["run"] = "planned"
    factory.executor.scripts["implement"] = APPEND
    factory.executor.scripts["describe"] = DESCRIPTION
    factory.executor.scripts["select"] = plan(["28"], {"29": "Blocked elsewhere."})
    factory.gh.issue(5, "Elsewhere", repo="owner/other")
    factory.gh.issue(28, "Greet")
    factory.gh.issue(29, "Stuck", blocked_by=["owner/other#5"])
    url = factory.gh.issue(21, "Greeting", sub_issues=[28, 29])

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert entered(factory, run_id) == ["select", "build", "describe", "open_pr", "ci"]
    assert factory.gh.pulls()[0]["title"] == DESCRIPTION.output["title"]
    # The Run freezes the Role, and a Role of the frozen Library still selects.
    frozen = yaml.safe_load((factory.run_dir(run_id) / "library" / "roles" / "planner.yaml").read_text())
    assert frozen["selects"] is True


PLANNER = {"role": "planner", "next": {"selected": "build", "empty": "ended", "failed": "ended"}}
TICKETS_THEN_PLANNER = {
    "start": "build",
    "nodes": {
        "build": {
            **ONLY_TICKETS["nodes"]["build"],
            "next": {"all_merged": "select", "some_merged": "ended", "none_merged": "ended"},
        },
        "select": {"role": "planner", "next": {"selected": "ended", "empty": "ended", "failed": "ended"}},
    },
}
PLANNER_AND_FILTER = {
    "start": "plan",
    "nodes": {
        "plan": {**PLANNER, "next": {"selected": "second", "empty": "ended", "failed": "ended"}},
        "second": {"kind": "filter", "with": {"label": READY}, "next": {"selected": "build", "empty": "ended"}},
        **ONLY_TICKETS["nodes"],
    },
}


@pytest.mark.parametrize(
    ("graph", "error"),
    [
        (TICKETS_THEN_PLANNER, "nodes.select: a path reaches this Stage after the tickets node"),
        (PLANNER_AND_FILTER, "nodes: a Run graph selects its Tickets once, and it has plan, second"),
        (
            {
                "start": "select",
                "nodes": {
                    "select": {**PLANNER, "next": {"selected": "build", "failed": "ended"}},
                    **ONLY_TICKETS["nodes"],
                },
            },
            "outcome empty has no route",
        ),
    ],
    ids=["after-tickets", "selecting-twice", "no-route-for-empty"],
)
def test_check_refuses_a_bad_planner_node(factory: Harness, graph: dict[str, Any], error: str) -> None:
    put_graph(factory, "default", graph)

    result = factory.check()

    assert result.code != 0
    assert error in result.err


def test_check_holds_the_role_of_a_planner_to_what_the_run_reads_of_it(factory: Harness) -> None:
    factory.pipeline["roles"]["planner"]["output"] = "schemas/describer.json"
    planning(factory)

    result = factory.check()

    assert result.code != 0
    assert "Role planner: the schema of the Planner must require tickets" in result.err


def test_a_role_that_says_selects_must_say_it_with_a_boolean(factory: Harness) -> None:
    factory.pipeline["roles"]["planner"]["selects"] = "yes"
    planning(factory)

    result = factory.check()

    assert result.code != 0
    assert "role planner in factory: `selects` must be true or false" in result.err
