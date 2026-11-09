"""A Run node kind is one registered object, which the loader and the checks read without naming it (ADR 0015)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from factory import wiring
from factory.pipeline.pipeline import RunNode
from factory.pipeline.pipeline_checks import check_run_graph
from factory.pipeline.run_kinds import AT_MOST_ONE, ONE, NodeRunner, Order, RunNodeKind, fixed, no_runner
from factory.pipeline_loader import GATE_KINDS
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
    check_run_graph(nodes, start, None, lambda where, message: found.append(f"{where}: {message}"), KINDS.values())
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

    def __init__(self, node: RunNode, tools: Any) -> None:
        self.node = node

    def step(self) -> str:
        Stamp.stepped.append(self.node.name)
        return "stamped"


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
