"""A Run node kind is one object that says what the nodes of that kind of a Run graph are and how one runs (ADR 0015).

`wiring` registers the kinds, and the loader, the checks, `factory diagram` and the Orchestrator read them from the
registry, so none of them keeps a table of kinds.
Each kind lives in one module of `run/nodes/`, which builds its RunNodeKind with the helpers here and its NodeRunner.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from factory.core import values

# What a kind's parser tells about a field that it rejects: the place in the file and the reason.
Report = Callable[[str, str], None]

# The shapes `factory diagram` draws a Run node of a kind in.
OPERATION_SHAPE = "operation"
WAIT_SHAPE = "wait"
TICKETS_SHAPE = "tickets"

# How many nodes of a kind, or of its group, a Run graph may hold.
ONE = "one"
AT_MOST_ONE = "at most one"


@dataclass(frozen=True)
class Group:
    """A Group is a name that several kinds share for their `count`, and `does` says what its nodes do, which names
    the group in the message of a graph that holds too many of them."""

    name: str
    does: str


def nothing_after(node: Any) -> tuple[str, ...]:
    return ()


@dataclass(frozen=True)
class Order:
    """An Order holds the rules of `factory check` for where the nodes of a kind stand in a Run graph.

    `count` says how many nodes of the kind, or of its `group`, the graph may hold.
    `after(node)` names the kinds that have run on every path to the node, and `before` the kinds that may not have
    run on any path to it.
    """

    count: str | None = None
    group: Group | None = None
    after: Callable[[Any], tuple[str, ...]] = nothing_after
    before: tuple[str, ...] = ()


def no_settings(where: str, body: Mapping[str, Any], report: Report) -> None:
    """Reads the body of a kind that takes no `with` and no `on_fail`."""
    with_block(where, body, report)


def no_labels(node: Any) -> tuple[str, ...]:
    return ()


class NodeRunner(Protocol):
    """A NodeRunner carries out one node of a Run graph.

    `step()` works the node until it ends and returns its outcome, or None for a Run the owner aborted.
    `finish()` does what `factory finish` asks of the node, and nothing by default.
    A runner keeps nothing between two steps that the Journal does not hold, so the Orchestrator makes one at each
    entry and after `resume`, and `step` does only what the Journal does not hold yet.
    """

    def step(self) -> str | None: ...

    def finish(self) -> None:
        return None


def no_runner(node: Any, tools: Any) -> NodeRunner:
    """Refuses to run a node of a kind that has no runner, which the loader reported already."""
    raise AssertionError(f"Config refuses {node.name}, whose kind {node.kind} has no runner")


@dataclass(frozen=True)
class RunNodeKind:
    """A RunNodeKind is everything the engine knows of one kind of Run node.

    `parse(where, body, report)` reads the `with` and `on_fail` of a node's body into the kind's own settings, which
    the RunNode holds, and reports what it rejects.
    `outcomes(node)` gives the closed outcomes of a node, which may depend on its settings.
    `labels(node)` names the labels the node needs on the Tracker, which `factory doctor` looks for.
    `holds_pr` tells whether a Run in such a node has its PR open, which `factory finish` reads.
    `shape` is how `factory diagram` draws the node.
    `make(node, tools)` builds the NodeRunner of a node from the NodeTools of the Run, which `run` defines, so this
    module takes them as Any.
    A kind is one of its name, so two kinds are equal when their names are.
    """

    name: str
    outcomes: Callable[[Any], tuple[str, ...]] = field(compare=False)
    parse: Callable[[str, Mapping[str, Any], Report], Any] = field(default=no_settings, compare=False)
    order: Order = field(default=Order(), compare=False)
    labels: Callable[[Any], tuple[str, ...]] = field(default=no_labels, compare=False)
    holds_pr: bool = field(default=False, compare=False)
    shape: str = field(default=OPERATION_SHAPE, compare=False)
    make: Callable[[Any, Any], NodeRunner] = field(default=no_runner, compare=False)


def fixed(*outcomes: str) -> Callable[[Any], tuple[str, ...]]:
    """Returns the `outcomes` of a kind whose nodes all end in the same outcomes."""
    return lambda node: outcomes


def unknown(name: str) -> RunNodeKind:
    """Returns the kind of a node whose `kind` no registered kind has, which the loader already reported, and whose
    `with` and `on_fail` it reports as fields that no kind takes."""
    return RunNodeKind(name=name, outcomes=fixed())


RunNodeKinds = Mapping[str, RunNodeKind]


@dataclass(frozen=True)
class With:
    """With is the `with` block a kind reads, after `with_block` checked its fields."""

    where: str
    fields: dict[str, Any] = field(default_factory=dict)


def with_block(
    where: str,
    body: Mapping[str, Any],
    report: Report,
    required: tuple[str, ...] = (),
    optional: tuple[str, ...] = (),
    on_fail: bool = False,
) -> With:
    """Returns the `with` of a node's body, and reports an `on_fail` on a kind that has none, a field it does not
    allow and a field it misses."""
    if not on_fail and "on_fail" in body:
        report(f"{where}.on_fail", "only a Tracker Operation has on_fail")
    at = f"{where}.with"
    fields = mapping(at, _with_on(body.get("with", {})), report)
    for key in fields:
        if key not in (*required, *optional):
            report(f"{at}.{key}", "unknown field")
    for key in required:
        if key not in fields:
            report(at, f"missing {key}")
    return With(at, fields)


def mapping(where: str, value: Any, report: Report) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    report(where, "must be a mapping")
    return {}


def names(at: str, fields: Mapping[str, Any], key: str, report: Report) -> tuple[str, ...]:
    """Returns the list of names under `key`, or nothing, with a report, when it is no list of names."""
    value = fields.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        report(f"{at}.{key}", "must be a list of names")
        return ()
    return tuple(value)


def parsed[T](where: str, value: Any, parse: Callable[[Any], T], report: Report) -> T | None:
    """Returns `value` parsed, or None, with a report, when it does not parse."""
    try:
        return parse(value)
    except values.Invalid as error:
        report(where, str(error))
        return None


def _with_on(with_: Any) -> Any:
    """Returns a `with` with its key `on` back, which YAML reads as a boolean when it has no quotes."""
    if not isinstance(with_, dict) or True not in with_:
        return with_
    return {"on" if key is True else key: value for key, value in with_.items()}
