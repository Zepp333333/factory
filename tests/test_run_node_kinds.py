"""A Run node kind is one registered object, which the loader and the checks read without naming it (ADR 0015)."""

from pathlib import Path
from typing import Any

import yaml

from factory.pipeline.pipeline import RunNode
from factory.pipeline.pipeline_checks import check_run_graph
from factory.pipeline.run_kinds import AT_MOST_ONE, ONE, Order, RunNodeKind, fixed
from factory.pipeline_loader import GATE_KINDS
from factory.wiring import FACTORY_LIBRARY, RUN_NODES

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
