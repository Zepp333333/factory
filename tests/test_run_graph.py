"""A Run walks a Run graph of the Library, in the format of a Pipeline, with node kinds of its own (ADR 0014)."""

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from factory.config import display_path, load_project
from factory.journal.journal import EventRow, Journal
from factory.journal.recovery import replay
from factory.run.live import LiveTicket
from harness import FACTORY_LIBRARY, AgentScript, FakeTty, Harness, run_id_of, ticket_text
from test_describer import DESCRIPTION, with_describer
from test_human_decider import asking
from test_recovery import (
    STALE,
    Crash,
    attempt_started,
    crash_when,
    crashed_run,
    killed_runs_run_no_handler,  # noqa: F401
)

HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})
TICKET = ticket_text("Greet")

# A Run graph that only builds, so its Run opens no PR.
ONLY_TICKETS: dict[str, Any] = {
    "start": "build",
    "nodes": {
        "build": {
            "kind": "tickets",
            "with": {"pipeline": "${vars.pipeline}"},
            "next": {"all_merged": "ended", "some_merged": "ended", "none_merged": "ended"},
        }
    },
}


WITHOUT_DESCRIBE: dict[str, Any] = {
    "start": "build",
    "nodes": {
        "build": {
            "kind": "tickets",
            "with": {"pipeline": "${vars.pipeline}"},
            "next": {"all_merged": "open_pr", "some_merged": "open_pr", "none_merged": "ended"},
        },
        "open_pr": {"kind": "open_pr", "next": {"opened": "ended", "failed": "ended"}},
    },
}


def put_graph(factory: Harness, name: str, graph: dict[str, Any]) -> Path:
    """Writes the Run graph `name` to the project Layer, and makes it the one the project file names."""
    factory.project["run"] = name
    path = factory.repo / ".factory" / "runs" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(graph))
    return path


def greeting(factory: Harness) -> Path:
    factory.executor.scripts["implement"] = HELLO
    return factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})


def walked(factory: Harness, run_id: str) -> list[tuple[str, str]]:
    """Returns the node and the outcome of each Run node that ended, in order."""
    return [(e["node"], e["outcome"]) for e in factory.events(run_id, "RunNodeEnded")]


def entered(factory: Harness, run_id: str) -> list[str]:
    return [e["node"] for e in factory.events(run_id, "RunNodeEntered")]


def test_a_project_file_of_today_walks_the_default_run_graph_and_shows_the_node_it_is_in(factory: Harness) -> None:
    with_describer(factory)
    result = factory.run(greeting(factory))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert entered(factory, run_id) == ["select", "build", "describe", "open_pr", "ci"]
    assert walked(factory, run_id) == [
        ("select", "selected"),
        ("build", "all_merged"),
        ("describe", "done"),
        ("open_pr", "opened"),
        ("ci", "absent"),
    ]
    assert [e["next"] for e in factory.events(run_id, "RunNodeEnded")] == [
        "build",
        "describe",
        "open_pr",
        "ci",
        "ended",
    ]
    assert factory.events(run_id, "RunStarted")[0]["settings"]["run"] == "default"
    assert len(factory.events(run_id, "PullRequestRecorded")) == 1
    assert (factory.run_dir(run_id) / "pull_request_title.txt").read_text() == "Append a line to the log\n"
    assert (factory.run_dir(run_id) / "pull_request.md").read_text().startswith(DESCRIPTION.output["body"].strip())
    assert "  Run node: build" in result.out.splitlines()
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"


def test_a_run_that_merges_no_ticket_goes_from_the_tickets_node_to_the_end(factory: Harness) -> None:
    factory.project.pop("run")
    factory.pipeline["defaults"]["sandbox_retries"] = 0
    factory.sandbox.refuse = 2

    result = factory.run(greeting(factory))

    assert result.code == 1, result.out + result.err
    assert walked(factory, run_id_of(result.out)) == [("select", "selected"), ("build", "none_merged")]


def test_a_project_runs_the_run_graph_of_its_own_layer_in_place_of_the_shipped_one(factory: Harness) -> None:
    put_graph(factory, "default", ONLY_TICKETS)

    result = factory.run(greeting(factory))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id) == [("build", "all_merged")]
    assert factory.events(run_id, "PullRequestRecorded") == []
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"


def test_run_names_another_run_graph_of_the_library(factory: Harness) -> None:
    put_graph(factory, "only-tickets", ONLY_TICKETS)

    result = factory.run(greeting(factory))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "RunStarted")[0]["settings"]["run"] == "only-tickets"
    assert walked(factory, run_id) == [("build", "all_merged")]


def test_overrides_change_a_field_of_a_node_of_the_shipped_run_graph(factory: Harness) -> None:
    with_describer(factory)
    factory.project["overrides"] = {"nodes": {"open_pr": {"next": {"opened": "ended"}}}}

    result = factory.run(greeting(factory))

    assert result.code == 0, result.out + result.err
    assert walked(factory, run_id_of(result.out)) == [
        ("select", "selected"),
        ("build", "all_merged"),
        ("describe", "done"),
        ("open_pr", "opened"),
    ]


def test_the_tickets_node_names_the_ticket_pipeline_and_an_override_changes_it(factory: Harness) -> None:
    other = {**factory.pipeline, "stages": {"build_it": {**factory.pipeline["stages"]["implement"]}}}
    other.pop("roles", None)
    other["start"] = "build_it"
    path = factory.repo / ".factory" / "pipelines" / "other.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(other))
    factory.project["overrides"] = {"nodes": {"build": {"with": {"pipeline": "other"}}}}
    factory.executor.scripts["build_it"] = HELLO

    result = factory.run(factory.write_spec("Greeting", "Say hello.", {"t1": TICKET}))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [e["node"] for e in factory.events(run_id, "AttemptStarted")] == ["build_it"]
    assert Path(factory.events(run_id, "RunStarted")[0]["pipeline"]).name == "other.yaml"


def test_vars_pipeline_of_a_project_file_stays_the_pipelines_own_variable(factory: Harness) -> None:
    with_describer(factory)
    factory.project["vars"] = {"pipeline": "not-a-pipeline"}

    result = factory.run(greeting(factory))

    assert result.code == 0, result.out + result.err
    assert entered(factory, run_id_of(result.out)) == ["select", "build", "describe", "open_pr", "ci"]


BUILD = {"kind": "tickets", "with": {"pipeline": "${vars.pipeline}"}}
ALL_TO_END = {"all_merged": "ended", "some_merged": "ended", "none_merged": "ended"}
PR_TO_END = {"opened": "ended", "failed": "ended"}


@pytest.mark.parametrize(
    ("graph", "errors"),
    [
        pytest.param(
            {"start": "build", "nodes": {"build": {**BUILD, "next": {"all_merged": "ended", "none_merged": "ended"}}}},
            ["nodes.build: outcome some_merged has no route"],
            id="unrouted-outcome",
        ),
        pytest.param(
            {
                "start": "pr",
                "nodes": {
                    "pr": {"kind": "open_pr", "next": PR_TO_END},
                    "build": {**BUILD, "next": ALL_TO_END},
                },
            },
            [
                "nodes.build: the start of the Run graph never reaches this tickets node",
                "nodes.pr: a path reaches this open_pr node before the tickets node",
            ],
            id="unreachable-tickets",
        ),
        pytest.param(
            {
                "start": "pr",
                "nodes": {
                    "pr": {"kind": "open_pr", "next": {"opened": "build", "failed": "build"}},
                    "build": {**BUILD, "next": ALL_TO_END},
                },
            },
            ["nodes.pr: a path reaches this open_pr node before the tickets node"],
            id="pr-before-tickets",
        ),
        pytest.param(
            {"start": "pr", "nodes": {"pr": {"kind": "open_pr", "next": PR_TO_END}}},
            [
                "nodes: a Run graph needs one node of kind tickets",
                "nodes.pr: a path reaches this open_pr node before the tickets node",
            ],
            id="no-tickets",
        ),
        pytest.param(
            {
                "start": "one",
                "nodes": {
                    "one": {**BUILD, "next": {**ALL_TO_END, "all_merged": "two"}},
                    "two": {**BUILD, "next": ALL_TO_END},
                },
            },
            ["nodes: a Run graph needs one node of kind tickets, and it has one, two"],
            id="two-tickets",
        ),
        pytest.param(
            {
                "start": "build",
                "nodes": {
                    "build": {**BUILD, "next": {**ALL_TO_END, "all_merged": "ci"}},
                    "ci": {"kind": "wait_ci", "next": {"green": "ci", "absent": "ended", "accepted": "ended"}},
                },
            },
            ["nodes.ci: outcome green loops back to this node, and the Run would never end"],
            id="loop",
        ),
        pytest.param(
            {"start": "build", "nodes": {"build": {**BUILD, "next": {**ALL_TO_END, "merged": "ended"}}}},
            ["nodes.build: there is no outcome merged"],
            id="unknown-outcome",
        ),
        pytest.param(
            {
                "start": "build",
                "nodes": {"build": {**BUILD, "next": {**ALL_TO_END, "all_merged": "nowhere"}}},
            },
            ["nodes.build: outcome all_merged routes to undeclared node nowhere"],
            id="undeclared-target",
        ),
        pytest.param(
            {"start": "build", "nodes": {"build": {"kind": "merge", "next": {}}}},
            ["nodes.build: unknown kind merge", "nodes: a Run graph needs one node of kind tickets"],
            id="kind-of-the-ticket-level",
        ),
        pytest.param(
            {"start": "build", "nodes": {"build": {"kind": "tickets", "next": ALL_TO_END}}},
            ["nodes.build.with: missing pipeline"],
            id="tickets-without-pipeline",
        ),
        pytest.param(
            {
                "start": "build",
                "nodes": {
                    "build": {**BUILD, "next": {**ALL_TO_END, "all_merged": "pr"}},
                    "pr": {"kind": "open_pr", "with": {"remote": "origin"}, "next": PR_TO_END},
                },
            },
            ["nodes.pr.with.remote: unknown field"],
            id="unknown-with",
        ),
        pytest.param(
            {"start": "absent", "stages": {}, "nodes": {"build": {**BUILD, "next": ALL_TO_END, "role": "implementer"}}},
            [
                "stages: unknown field",
                "nodes.build.role: unknown field",
                "start: names undeclared node absent",
            ],
            id="fields-of-the-ticket-level",
        ),
    ],
)
def test_check_rejects_a_run_graph_that_breaks_the_order_of_a_run(
    factory: Harness, graph: dict[str, Any], errors: list[str]
) -> None:
    path = put_graph(factory, "default", graph)

    result = factory.check()

    assert result.code == 2
    prefix = f"factory: {display_path(path)}: "
    assert [line.removeprefix(prefix) for line in result.err.splitlines()] == errors


def test_check_rejects_a_tickets_node_in_a_pipeline(factory: Harness) -> None:
    factory.pipeline["operations"]["merge"]["kind"] = "tickets"

    result = factory.check()

    assert result.code == 2
    assert "operations.merge: unknown kind tickets" in result.err


@pytest.mark.parametrize(
    ("project", "error"),
    [
        pytest.param({"run": "absent"}, "run: Run graph absent is in no Layer of the Library", id="unknown-name"),
        pytest.param({"run": {"file": "graph.yaml"}}, "run: must be a Run graph name", id="not-a-name"),
        pytest.param(
            {"overrides": {"nodes": {"nope": {"with": {}}}}},
            "overrides.nodes.nope: there is no such Run node",
            id="override-of-an-unknown-node",
        ),
        pytest.param(
            {"overrides": {"nodes": {"build": {"tone": "dry"}}}},
            "overrides.nodes.build.tone: unknown field",
            id="override-of-an-unknown-field",
        ),
    ],
)
def test_check_rejects_what_a_project_file_says_of_its_run_graph(
    factory: Harness, project: dict[str, Any], error: str
) -> None:
    factory.project.update(project)

    result = factory.check()

    assert result.code == 2
    assert result.err.splitlines() == [f"factory: {display_path(factory.project_file)}: {error}"]


def test_library_show_run_prints_the_run_graph_and_the_layer_it_came_from(factory: Harness) -> None:
    project = ("--project", str(factory.project_file))
    shipped = factory.cli("library", "show", "run", "default", *project)
    assert shipped.code == 0, shipped.err
    first, _, rest = shipped.out.partition("\n")
    assert first == "# Run graph default (factory)"
    assert yaml.safe_load(rest) == yaml.safe_load((FACTORY_LIBRARY / "runs" / "default.yaml").read_text())

    put_graph(factory, "default", ONLY_TICKETS)
    replaced = factory.cli("library", "show", "run", "default", *project)
    assert replaced.out.splitlines()[0] == "# Run graph default (project)"
    assert yaml.safe_load(replaced.out)["nodes"]["build"]["next"] == ONLY_TICKETS["nodes"]["build"]["next"]

    unknown = factory.cli("library", "show", "run", "absent", *project)
    assert unknown.code == 2
    assert "Run graph absent is in no Layer" in unknown.err

    outside = factory.cli("library", "show", "run", "default", "--project", str(factory.root / "factory.yaml"))
    assert outside.out.splitlines()[0] == "# Run graph default (factory)"


def test_a_run_killed_in_the_tickets_node_resumes_in_it_on_the_run_graph_it_froze(factory: Harness) -> None:
    stuck = AgentScript(files={"junk.txt": "half\n"}, output=HELLO.output, wait_for=factory.root / "go")
    factory.executor.scripts["implement"] = [stuck, HELLO]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    run_id = crashed_run(factory, spec, attempt_started(factory))
    factory.clock.sleep(STALE.seconds)
    put_graph(factory, "default", ONLY_TICKETS)

    assert "Run node: build" in factory.cli("status", run_id).out.splitlines()
    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert entered(factory, run_id) == ["select", "build", "open_pr", "ci"]
    assert walked(factory, run_id)[1] == ("build", "all_merged")


def test_a_run_killed_in_the_describe_node_resumes_in_it(factory: Harness) -> None:
    with_describer(factory)
    stuck = AgentScript(output=DESCRIPTION.output, wait_for=factory.root / "go")
    factory.executor.scripts["describe"] = [stuck, DESCRIPTION]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    run_id = crashed_run(factory, spec, lambda run_id: bool(factory.events(run_id, "DescriberStarted")))
    factory.clock.sleep(STALE.seconds)

    assert "Run node: describe" in factory.cli("status", run_id).out.splitlines()
    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert entered(factory, run_id) == ["select", "build", "describe", "open_pr", "ci"]
    assert [node for node, _ in walked(factory, run_id)] == ["select", "build", "describe", "open_pr", "ci"]


def test_a_run_killed_in_the_open_pr_node_hands_the_tracker_on_resume_what_its_last_delivery_failed_to(
    factory: Harness,
) -> None:
    asking(factory)
    with_describer(factory)
    stuck = AgentScript(output=DESCRIPTION.output, wait_for=factory.root / "go")
    factory.executor.scripts["describe"] = [stuck, DESCRIPTION]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    (spec / "projection").write_text("not a folder\n")
    tty = factory.stdin = FakeTty()

    def on_sleep(seconds: float) -> None:
        run_ids = factory.run_ids()
        if factory.waiting():
            tty.type("accept\n")
        if run_ids and factory.events(run_ids[-1], "DescriberStarted"):
            factory.clock.on_sleep = None
            raise Crash

    factory.clock.on_sleep = on_sleep
    with pytest.raises(Crash):
        factory.run(spec)
    factory.crashed()
    tty.close()
    run_id = factory.run_ids()[-1]
    assert factory.events(run_id, "TrackerDeliveryFailed")
    assert factory.events(run_id, "TrackerDelivered") == []
    (spec / "projection").unlink()
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert [e["type"] for e in factory.events(run_id, "TrackerDelivered")] == ["DecisionWaiting", "DecisionDecided"]


def test_a_run_killed_in_the_wait_ci_node_resumes_in_it(factory: Harness) -> None:
    factory.detach_remotes = False
    spec = greeting(factory)
    factory.gh.checks([{"name": "unit", "bucket": "pending", "link": "https://ci"}], [])
    run_id = crashed_run(factory, spec, lambda run_id: bool(factory.events(run_id, "CiStarted")))
    factory.clock.sleep(STALE.seconds)
    assert "Run node: ci" in factory.cli("status", run_id).out.splitlines()
    factory.gh.checks([{"name": "unit", "bucket": "pass", "link": "https://ci"}])

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert entered(factory, run_id) == ["select", "build", "open_pr", "ci"]
    assert walked(factory, run_id)[-1] == ("ci", "green")


def test_a_run_killed_between_two_nodes_resumes_in_the_next_one(factory: Harness) -> None:
    with_describer(factory)
    stuck = AgentScript(output=DESCRIPTION.output, wait_for=factory.root / "go")
    factory.executor.scripts["describe"] = [stuck, DESCRIPTION]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    run_id = crashed_run(factory, spec, lambda run_id: bool(factory.events(run_id, "DescriberStarted")))
    with contextlib.closing(sqlite3.connect(factory.home / "factory.db")) as db, db:
        db.execute(
            "DELETE FROM events WHERE run_id = ? AND type = 'RunNodeEntered' AND payload LIKE '%describe%'", (run_id,)
        )
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert entered(factory, run_id) == ["select", "build", "describe", "open_pr", "ci"]
    assert len(factory.events(run_id, "AttemptStarted")) == 1


def strip_run_nodes(factory: Harness, run_id: str) -> None:
    """Leaves the Journal and the frozen Library of a Run as a factory from before Run graphs left them."""
    with contextlib.closing(sqlite3.connect(factory.home / "factory.db")) as db, db:
        db.execute(
            "DELETE FROM events WHERE run_id = ? AND (type LIKE 'RunNode%' OR type = 'TicketsSelected')", (run_id,)
        )
        db.execute(
            "UPDATE events SET payload = json_remove(payload, '$.settings.run')"
            " WHERE run_id = ? AND type = 'RunStarted'",
            (run_id,),
        )
    runs = factory.home / "runs" / run_id / "library" / "runs"
    for path in runs.iterdir():
        path.chmod(0o644)
        path.unlink()
    runs.rmdir()


def test_a_journal_from_before_run_graphs_replays_to_the_same_state(factory: Harness) -> None:
    run_id = run_id_of(factory.run(greeting(factory)).out)
    journal = Journal(factory.home / "factory.db", factory.clock)
    events = journal.events(run_id)
    journal.close()
    pipeline = load_project(factory.project_file, factory.machine().library, factory.machine().kinds).pipeline

    def state(kept: list[EventRow]) -> dict[str, Any]:
        memory = replay(pipeline, [LiveTicket(id="t1", blocked_by=[])], kept, 1, executor="claude-code")
        return {key: value for key, value in memory.comparable().items() if key not in ("run_node", "run_next")}

    assert state([e for e in events if not e.type.startswith("RunNode")]) == state(events)


def test_a_run_from_before_run_graphs_resumes_as_the_default_run_graph(factory: Harness) -> None:
    factory.executor.scripts["describe"] = DESCRIPTION
    stuck = AgentScript(files={"junk.txt": "half\n"}, output=HELLO.output, wait_for=factory.root / "go")
    factory.executor.scripts["implement"] = [stuck, HELLO]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    run_id = crashed_run(factory, spec, attempt_started(factory))
    strip_run_nodes(factory, run_id)
    factory.clock.sleep(STALE.seconds)
    # What the project file says by now does not reach a Run that recorded no `run`.
    put_graph(factory, "only-tickets", ONLY_TICKETS)

    assert not any(line.startswith("Run node:") for line in factory.cli("status", run_id).out.splitlines())
    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert walked(factory, run_id) == [
        ("select", "selected"),
        ("build", "all_merged"),
        ("describe", "done"),
        ("open_pr", "opened"),
        ("ci", "absent"),
    ]
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"


def test_finish_of_a_run_whose_graph_has_no_open_pr_node_opens_no_pull_request(factory: Harness) -> None:
    put_graph(factory, "default", ONLY_TICKETS)
    factory.executor.scripts["implement"] = [HELLO]
    factory.executor.ticket_scripts[("t2", "implement")] = [
        AgentScript(files={"junk.txt": "half\n"}, output=HELLO.output, wait_for=factory.root / "go")
    ]
    spec = factory.write_spec("Pair", "Two.", {"t1": TICKET, "t2": ticket_text("Other", ["t1"])})
    crash_when(factory, lambda run_id: len(factory.events(run_id, "AttemptStarted")) == 2)
    with pytest.raises(Crash):
        factory.run(spec)
    factory.crashed()
    run_id = factory.run_ids()[-1]
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("finish", run_id)

    assert f"Run {run_id} finished: partial" in result.out
    assert factory.events(run_id, "PullRequestRecorded") == []


def test_a_run_graph_without_describe_passes_check_and_opens_the_pr_with_the_trackers_text(factory: Harness) -> None:
    put_graph(factory, "plain", WITHOUT_DESCRIBE)

    assert factory.check().code == 0
    result = factory.run(greeting(factory))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id) == [("build", "all_merged"), ("open_pr", "opened")]
    assert factory.events(run_id, "DescriberStarted") == []
    assert (factory.run_dir(run_id) / "pull_request_title.txt").read_text() == "Greeting: Greet\n"


def test_stats_shows_describe_with_its_cost_and_each_run_node_with_its_duration_and_cost(factory: Harness) -> None:
    with_describer(factory)
    factory.executor.scripts["implement"] = HELLO
    factory.executor.scripts["describe"] = AgentScript(output=DESCRIPTION.output, cost=0.25)
    run_id = run_id_of(factory.run(factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})).out)

    lines = factory.cli("stats", run_id).out.splitlines()

    [row] = [line for line in lines if line.startswith("describe ") and "0.25 / 0.25" in line]
    assert row.split()[1] == "1"
    [node] = [line for line in lines if line.startswith("describe ") and "done" in line]
    assert node.split()[1] == "done" and node.endswith("0.25")
    assert any(line.startswith("run node") for line in lines)
    by_model = factory.cli("stats", run_id, "--by", "model").out.splitlines()
    assert any(line.split()[:2] == ["describe", "sonnet"] for line in by_model)
    data = json.loads(factory.cli("stats", run_id, "--json").out)
    [run] = data["runs"]
    assert [n["node"] for n in run["run_nodes"]] == ["select", "build", "describe", "open_pr", "ci"]
    assert run["run_nodes"][2]["cost_usd"] == 0.25
    assert all(n["duration_s"] is not None for n in run["run_nodes"])
    assert run["cost_usd"] == 0.25


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"kind": "land", "next": {"opened": "ended", "failed": "ended", "empty": "ended"}}, "kind land is now"),
        ({"role": "describer", "next": {"done": "ended"}, "inputs": []}, "inputs: unknown field"),
    ],
)
def test_check_rejects_a_retired_or_unknown_field_of_a_run_node(
    factory: Harness, body: dict[str, Any], expected: str
) -> None:
    put_graph(factory, "plain", {**WITHOUT_DESCRIBE, "nodes": {**WITHOUT_DESCRIBE["nodes"], "open_pr": body}})

    result = factory.check()

    assert result.code != 0
    assert expected in result.err + result.out


def test_check_names_the_run_graph_node_that_replaces_the_describer_field(factory: Harness) -> None:
    factory.pipeline.update({"describer": {"role": "describer"}})

    result = factory.check()

    assert result.code != 0
    assert "describer: is now the node `describe` of the Run graph" in result.err + result.out


def test_finish_of_a_run_in_the_default_graph_describes_and_opens_the_pr_with_the_describers_text(
    factory: Harness,
) -> None:
    with_describer(factory)
    factory.executor.scripts["implement"] = [HELLO]
    factory.executor.ticket_scripts[("t2", "implement")] = [
        AgentScript(files={"junk.txt": "half\n"}, output=HELLO.output, wait_for=factory.root / "go")
    ]
    spec = factory.write_spec("Pair", "Two.", {"t1": TICKET, "t2": ticket_text("Other", ["t1"])})
    crash_when(factory, lambda run_id: len(factory.events(run_id, "AttemptStarted")) == 2)
    with pytest.raises(Crash):
        factory.run(spec)
    factory.crashed()
    run_id = factory.run_ids()[-1]
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("finish", run_id)

    assert f"Run {run_id} finished: partial" in result.out
    assert (factory.run_dir(run_id) / "pull_request_title.txt").read_text() == "Append a line to the log\n"


DESCRIBE = {"role": "describer", "next": {"done": "ended", "failed": "ended"}}


@pytest.mark.parametrize(
    ("graph", "errors"),
    [
        pytest.param(
            {
                "start": "describe",
                "nodes": {
                    "describe": {"role": "describer", "next": {"done": "build", "failed": "build"}},
                    "build": {**BUILD, "next": ALL_TO_END},
                },
            },
            ["nodes.describe: a path reaches this Stage before the tickets node"],
            id="before-tickets",
        ),
        pytest.param(
            {
                "start": "build",
                "nodes": {
                    "build": {**BUILD, "next": {**ALL_TO_END, "all_merged": "pr"}},
                    "pr": {"kind": "open_pr", "next": {"opened": "describe", "failed": "ended"}},
                    "describe": DESCRIBE,
                },
            },
            ["nodes.describe: its text comes after the open_pr node pr, which takes it"],
            id="after-open-pr",
        ),
        pytest.param(
            {
                "start": "build",
                "nodes": {
                    "build": {**BUILD, "next": {**ALL_TO_END, "all_merged": "one"}},
                    "one": {"role": "describer", "next": {"done": "two", "failed": "two"}},
                    "two": DESCRIBE,
                },
            },
            ["nodes: a Run graph has at most one Stage, the Describer, and it has one, two"],
            id="two-stages",
        ),
    ],
)
def test_check_rejects_a_run_stage_out_of_its_order(factory: Harness, graph: dict[str, Any], errors: list[str]) -> None:
    path = put_graph(factory, "default", graph)

    result = factory.check()

    assert result.code == 2
    prefix = f"factory: {display_path(path)}: "
    assert [line.removeprefix(prefix) for line in result.err.splitlines()] == errors
