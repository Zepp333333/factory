"""A Run node kind is one registered object, which the loader and the checks read without naming it (ADR 0015)."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from factory import wiring
from factory.core.events import Event
from factory.core.run_agent import RUN_AGENTS, RunAgent
from factory.journal.journal import Journal
from factory.pipeline.pipeline import RunNode, stage_outcomes
from factory.pipeline.pipeline_checks import check_run_graph
from factory.pipeline.run_kinds import AT_MOST_ONE, ONE, NodeRunner, Order, RunNodeKind, fixed, no_runner
from factory.pipeline_loader import GATE_KINDS
from factory.views.run_view import phase_note, run_phase
from factory.wiring import FACTORY_LIBRARY, RUN_NODES
from harness import AgentScript, Harness, put_graph, run_id_of, ticket_text

START = RunNodeKind(name="start", outcomes=fixed("done"), order=Order(count=ONE))
STAMP = RunNodeKind(name="stamp", outcomes=fixed("done"), order=Order(count=AT_MOST_ONE, after=lambda node: ("start",)))
SEAL = RunNodeKind(name="seal", outcomes=fixed("done"), order=Order(before=("stamp",)))
KINDS = {kind.name: kind for kind in (START, STAMP, SEAL)}


def errors_of(graph: dict[str, tuple[str, str]], start: str) -> list[str]:
    """Returns what the check reports of a graph of the fake kinds, whose nodes are named by their kind and target."""
    nodes: dict[str, Any] = {
        name: RunNode(name=name, spec=KINDS[kind], next={"done": target}) for name, (kind, target) in graph.items()
    }
    found: list[str] = []
    check_run_graph(nodes, start, None, lambda where, message: found.append(f"{where}: {message}"), KINDS)
    return found


def test_every_kind_that_a_shipped_run_graph_names_is_registered() -> None:
    named = set()
    for path in (FACTORY_LIBRARY / "runs").glob("*.yaml"):
        for body in yaml.safe_load(Path(path).read_text())["nodes"].values():
            if "kind" in body and "decider" not in body:
                named.add(body["kind"])
    assert named - set(GATE_KINDS) <= RUN_NODES.keys()
    assert {"tickets", "open_pr", "wait_ci"} <= named


def test_the_check_holds_each_node_to_the_order_of_its_kind_without_naming_the_kind() -> None:
    in_order = {"begin": ("start", "seal"), "seal": ("seal", "stamp"), "stamp": ("stamp", "ended")}
    assert errors_of(in_order, "begin") == []

    broken = {
        "first": ("stamp", "second"),
        "second": ("stamp", "begin"),
        "begin": ("start", "seal"),
        "seal": ("seal", "ended"),
    }
    assert errors_of(broken, "first") == [
        "nodes: a Run graph has at most one node of kind stamp, and it has first, second",
        "nodes.first: a path reaches this stamp node before the start node",
        "nodes.second: a path reaches this stamp node before the start node",
        "nodes.seal: a path reaches this seal node after the stamp node",
    ]
    assert errors_of({"seal": ("seal", "ended")}, "seal") == ["nodes: a Run graph needs one node of kind start"]


def test_every_registered_kind_makes_a_runner() -> None:
    assert [name for name, kind in RUN_NODES.items() if kind.make is no_runner] == []


class Stamp(NodeRunner):
    """Stamp is the runner of a fake kind, which keeps the names of the nodes it stepped."""

    stepped: list[str] = []
    outcome = "stamped"

    def __init__(self, node: Any, tools: Any) -> None:
        self.node = node

    def step(self) -> str:
        Stamp.stepped.append(self.node.name)
        return self.outcome


class Review(Stamp):
    """Review is the runner of a fake agent kind, whose Stage ends `done`."""

    outcome = "done"


def test_the_orchestrator_runs_a_registered_kind_it_never_names_through_the_runner_the_kind_makes(
    factory: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(wiring.RUN_NODES, "stamp", RunNodeKind(name="stamp", outcomes=fixed("stamped"), make=Stamp))
    monkeypatch.setattr(Stamp, "stepped", [])
    put_graph(
        factory,
        "stamped",
        {
            "start": "mark",
            "nodes": {
                "mark": {"kind": "stamp", "next": {"stamped": "build"}},
                "build": {
                    "kind": "tickets",
                    "with": {"pipeline": "${vars.pipeline}"},
                    "next": {"all_merged": "ended", "some_merged": "ended", "none_merged": "ended"},
                },
            },
        },
    )
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"}
    )

    result = factory.run(factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("Greet")}))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert Stamp.stepped == ["mark"]
    assert [(e["node"], e["kind"]) for e in factory.events(run_id, "RunNodeEntered")] == [
        ("mark", "stamp"),
        ("build", "tickets"),
    ]
    assert [(e["node"], e["outcome"]) for e in factory.events(run_id, "RunNodeEnded")] == [
        ("mark", "stamped"),
        ("build", "all_merged"),
    ]


@pytest.fixture
def reviewer() -> Iterator[RunAgent]:
    """Registers a Run-level agent that no module of the factory declares, as the module of a new agent kind would."""
    agent = RUN_AGENTS.register(
        RunAgent(
            name="reviewer",
            title="Reviewer",
            stage="review",
            sandbox="reviewer",
            started=(Event.FixStarted,),
            ends=(Event.FixFinished,),
            doing=lambda started, attempt: f"review: the Reviewer reads the PR, Attempt {attempt}",
            requires=(),
        )
    )
    yield agent
    RUN_AGENTS.unregister(agent.name)


def test_a_run_level_agent_registered_outside_the_factory_holds_its_slot_and_tells_the_status_while_it_works(
    factory: Harness, reviewer: RunAgent
) -> None:
    journal = Journal(factory.home / "factory.db", factory.clock)
    journal.append("r1", Event.RunStarted, {"spec_ref": "spec", "integration_branch": "factory/r1"})
    with journal.transaction():
        assert journal.take_slot("r1", reviewer.slot, cap=1)
        journal.append("r1", Event.FixStarted, {"agent": reviewer.name, "round": 1})
        journal.append(
            "r1", Event.AttemptStarted, {"ticket": reviewer.slot, "node": "review", "round": 1, "attempt": 2}
        )
    run = journal.run("r1")
    assert run is not None

    working = (
        run_phase(run, journal.events("r1")),
        phase_note(run, journal.events("r1"), factory.clock.now(), timed=False),
    )
    with journal.transaction():
        held = not journal.take_slot("r1", "t1", cap=1)
        journal.append("r1", Event.FixFinished, {"agent": reviewer.name, "round": 1})
        freed = journal.take_slot("r1", "t1", cap=1)

    assert working == ("review", "review: the Reviewer reads the PR, Attempt 2")
    assert (held, freed) == (True, True)
    assert run_phase(run, journal.events("r1")) == "running"
    journal.close()


def test_a_second_run_level_agent_of_one_name_is_refused(reviewer: RunAgent) -> None:
    with pytest.raises(ValueError, match="a Run-level agent named reviewer is registered already"):
        RUN_AGENTS.register(reviewer)


def test_a_run_stage_of_an_agent_kind_the_orchestrator_never_names_runs_through_the_runner_the_kind_makes(
    factory: Harness, monkeypatch: pytest.MonkeyPatch, reviewer: RunAgent
) -> None:
    kind = RunNodeKind(name="review_pr", outcomes=stage_outcomes, agent=reviewer, make=Review)
    monkeypatch.setitem(wiring.RUN_NODES, "review_pr", kind)
    monkeypatch.setattr(Stamp, "stepped", [])
    build = {"kind": "tickets", "with": {"pipeline": "${vars.pipeline}"}}
    put_graph(
        factory,
        "reviewed",
        {
            "start": "build",
            "nodes": {
                "build": {**build, "next": {"all_merged": "review", "some_merged": "ended", "none_merged": "ended"}},
                "review": {"kind": "review_pr", "role": "ci-fixer", "next": {"done": "ended", "failed": "ended"}},
            },
        },
    )
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"}
    )

    result = factory.run(factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("Greet")}))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert Stamp.stepped == ["review"]
    assert [(e["node"], e["kind"]) for e in factory.events(run_id, "RunNodeEntered")] == [
        ("build", "tickets"),
        ("review", "review_pr"),
    ]
