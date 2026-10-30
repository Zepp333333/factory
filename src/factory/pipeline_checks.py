"""Pipeline checks walk the graph of a parsed Pipeline and report what would break a Run on some path."""

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from factory.deciders import FUNCTIONS
from factory.library_lookup import Read
from factory.pipeline import (
    BUILTIN_INPUTS,
    ESCALATING_OUTCOMES,
    OPERATION_ARTIFACT_OUTCOMES,
    TERMINAL_NODES,
    Decision,
    Describer,
    Gate,
    Input,
    Node,
    Operation,
    Role,
    Stage,
)

Report = Callable[[str, str], None]


def check(nodes: dict[str, Node], start: str, escalate_to: str | None, report: Report) -> None:
    """Reports every graph error of the Pipeline as a place in the Pipeline file and a message."""
    if escalate_to is None:
        for node in nodes.values():
            if isinstance(node, Gate):
                report(f"gates.{node.name}", "a lost Probe escalates, so the Pipeline needs escalate_to")
    _check_routes(nodes, escalate_to, report)
    _check_conflicts(nodes, report)
    _check_exhaustion(nodes, escalate_to, report)
    if start in nodes:
        graph = _Graph(nodes, start, escalate_to)
        _check_inputs(nodes, graph, report)
        _check_sources(nodes, graph, report)
        _check_resets(nodes, graph, report)


def check_roles(nodes: dict[str, Node], describer: Describer | None, report: Report) -> None:
    """Checks the final schema of every Role against the engine's output contract and the reads of the Role (ADR 0010).

    A Role that failed to load has no schema, and the error of its loading already says why.
    """
    stages = [node for node in nodes.values() if isinstance(node, Stage)]
    for node in nodes.values():
        where = f"{node.KIND}s.{node.name}"
        if isinstance(node, Decision) and node.decider.function in FUNCTIONS:
            _check_function_reads(where, node, FUNCTIONS[node.decider.function].reads, nodes, report)
        role = _role_of(node)
        if role is None or role.schema is None:
            continue
        if isinstance(node, Stage):
            _check_summary(where, role, report)
        elif isinstance(node, Decision) and node.decider.kind == "model":
            _check_outcome(where, role, node.outcomes, report)
        _check_reads(where, role, node.inputs, nodes, report)
    if describer is not None and describer.role.schema is not None:
        role, schema = describer.role, describer.role.schema
        for field in ("title", "body"):
            if field not in schema.get("required", []):
                report("describer", f"Role {role.name}: the schema of the Describer must require {field}")
        _check_describer_reads(role, stages, report)


def _role_of(node: Node) -> Role | None:
    """Returns the Role of a Stage or of a model Decision, or None for a node that runs no agent."""
    if isinstance(node, Stage):
        return node.role
    if isinstance(node, Decision) and node.decider.kind == "model":
        return node.decider.role
    return None


def _check_summary(where: str, role: Role, report: Report) -> None:
    assert role.schema is not None
    summary = role.schema.get("properties", {}).get("summary")
    if summary is not None and _type_of(summary) != "string":
        report(where, f"Role {role.name}: the summary of a Stage must be a string")


def _check_outcome(where: str, role: Role, outcomes: tuple[str, ...], report: Report) -> None:
    assert role.schema is not None
    outcome = role.schema.get("properties", {}).get("outcome")
    values = outcome.get("enum") if isinstance(outcome, dict) else None
    if not isinstance(values, list) or "outcome" not in role.schema.get("required", []):
        report(where, f"Role {role.name}: the schema of a model Decider must require outcome as an enum")
    elif not set(values) <= set(outcomes):
        extra = ", ".join(str(value) for value in values if value not in outcomes)
        report(where, f"Role {role.name}: the enum of outcome has {extra}, which is no outcome of the Decision")


def _check_function_reads(
    where: str, node: Decision, reads: dict[str, tuple[str, ...]], nodes: dict[str, Node], report: Report
) -> None:
    """Reports a function Decision that no Stage it reads can feed with the fields in the values the function reads.

    The function takes the latest output that carries them all, so one feeding Stage is enough.
    """
    if not reads:
        return
    names = [name for item in node.inputs for name in item.nodes if name in nodes]
    roles = [role for role in (_role_of(nodes[name]) for name in names) if role is not None]
    if not roles or any(role.schema is None for role in roles):
        return
    if not any(all(_can_output(role, name, values) for name, values in reads.items()) for role in roles):
        wanted = " and ".join(f"{name} as {' or '.join(values)}" for name, values in reads.items())
        report(where, f"no input Stage can output {wanted}")


def _can_output(role: Role, field: str, values: tuple[str, ...]) -> bool:
    """Tells whether the final schema of a Role can output the field with one of the values."""
    assert role.schema is not None
    definition = role.schema.get("properties", {}).get(field)
    if definition is None:
        return role.schema.get("additionalProperties") is not False
    if not isinstance(definition, dict):
        return False
    allowed = definition.get("enum")
    if isinstance(allowed, list):
        return any(value in values for value in allowed)
    return _type_of(definition) in (None, "string")


def _type_of(definition: Any) -> str | None:
    """Returns the JSON type a property definition names, or None when it names none the check can compare."""
    if not isinstance(definition, dict):
        return None
    if isinstance(definition.get("type"), str):
        return str(definition["type"])
    values = definition.get("enum")
    if isinstance(values, list) and values and all(isinstance(value, str) for value in values):
        return "string"
    return None


def _check_reads(where: str, role: Role, inputs: tuple[Input, ...], nodes: dict[str, Node], report: Report) -> None:
    """Checks the reads of a Role against the Roles on the other side of the `inputs` of its node.

    A required field must be required in the schema of every node of some non-optional input,
    which is how a required input is guaranteed on every path.
    An optional field must only match in type wherever some input has it.
    """
    sources: list[tuple[Input, list[tuple[str, Role]]]] = []
    for item in inputs:
        providers = [(name, _role_of(nodes[name])) for name in item.nodes if name in nodes]
        known = [
            (name, provider) for name, provider in providers if provider is not None and provider.schema is not None
        ]
        if known and len(known) == len(providers):
            sources.append((item, known))
    for field, read in role.reads.items():
        if read.optional:
            for _, known_roles in sources:
                for problem in _mismatches(field, read, known_roles):
                    report(where, f"Role {role.name} reads optional field {field}, but {problem}")
            continue
        problems: list[str] = []
        for item, known_roles in sources:
            gaps = _gaps(field, read, known_roles)
            if item.optional:
                gaps.append(f"input {item} is optional")
            if not gaps:
                break
            problems.extend(gaps)
        else:
            why = "; ".join(problems) if problems else "no input is the output of a Stage"
            report(where, f"Role {role.name} reads required field {field}, but {why}")


def _gaps(field: str, read: Read, providers: list[tuple[str, Role]]) -> list[str]:
    """Returns why the nodes of an input do not always give the field, which is nothing when they do."""
    problems = []
    for name, provider in providers:
        assert provider.schema is not None
        definition = provider.schema.get("properties", {}).get(field)
        if definition is None:
            problems.append(f"the schema of Role {provider.name} of {name} has no field {field}")
        elif _type_of(definition) not in (None, read.type):
            problems.append(f"{name} outputs {field} as {_type_of(definition)}, not {read.type}")
        elif field not in provider.schema.get("required", []):
            problems.append(f"{name} does not always output {field}")
    return problems


def _mismatches(field: str, read: Read, providers: list[tuple[str, Role]]) -> list[str]:
    """Returns each node of an input that outputs the field as another type than the Role reads it as."""
    found = []
    for name, provider in providers:
        assert provider.schema is not None
        kind = _type_of(provider.schema.get("properties", {}).get(field))
        if kind not in (None, read.type):
            found.append(f"{name} outputs {field} as {kind}, not {read.type}")
    return found


def _check_describer_reads(role: Role, stages: list[Stage], report: Report) -> None:
    """Checks the reads of the Describer, which has no inputs, against the output of every Stage of the Pipeline.

    A required field must be output by at least one Stage, with the type the Describer reads it as.
    """
    providers = [(stage.name, stage.role) for stage in stages if stage.role.schema is not None]
    for field, read in role.reads.items():
        mismatches = _mismatches(field, read, providers)
        for problem in mismatches:
            report("describer", f"Role {role.name} reads {field}, but {problem}")
        if read.optional:
            continue
        outputs = [
            name
            for name, provider in providers
            if provider.schema is not None
            and field in provider.schema.get("properties", {})
            and _type_of(provider.schema["properties"][field]) in (None, read.type)
        ]
        if not outputs:
            report("describer", f"Role {role.name} reads required field {field}, but no Stage outputs it")


def merges_without_sync(nodes: dict[str, Node], start: str, escalate_to: str | None) -> list[tuple[str, str]]:
    """Returns each node that reaches a `merge` with no `sync` on the way, paired with that `merge`.

    A Ticket takes the Merge window at its first Operation and keeps it through Gates and Operations,
    so the Integration branch can move under a Ticket until a `sync` in that stretch brings it in.
    The walk starts after each node that does not hold the window, and at the start node.
    """
    graph = _Graph(nodes, start, escalate_to)
    found = []
    for source in nodes.values():
        if source.HOLDS_WINDOW and source.name != start:
            continue
        todo = [source.name] if source.HOLDS_WINDOW else graph.successors(source.name)
        seen: set[str] = set()
        while todo:
            name = todo.pop()
            node = nodes.get(name)
            if name in seen or node is None or not node.HOLDS_WINDOW:
                continue
            seen.add(name)
            if isinstance(node, Operation) and node.kind == "sync":
                continue
            if isinstance(node, Operation) and node.kind == "merge":
                found.append((f"{source.KIND}s.{source.name}", f"operations.{name}"))
            todo.extend(graph.successors(name))
    return found


def _check_routes(nodes: dict[str, Node], escalate_to: str | None, report: Report) -> None:
    known = set(nodes) | set(TERMINAL_NODES)
    for node in nodes.values():
        where = f"{node.KIND}s.{node.name}"
        for outcome in node.outcomes:
            target = node.next.get(outcome)
            if target is None and not (outcome in ESCALATING_OUTCOMES and escalate_to is not None):
                report(where, f"outcome {outcome} has no route")
            elif target is not None and target not in known:
                report(where, f"outcome {outcome} routes to undeclared node {target}")
        for outcome in node.next:
            if outcome not in node.outcomes:
                report(where, f"there is no outcome {outcome}")
        for outcome, names in node.resets.items():
            for name in names:
                stage = nodes.get(name)
                if not isinstance(stage, Stage):
                    report(f"{where}.next.{outcome}", f"reset_rounds names {name}, which is not a Stage")
                elif stage.max_rounds is None:
                    report(f"{where}.next.{outcome}", f"reset_rounds names {name}, which has no rounds limit")


def _check_conflicts(nodes: dict[str, Node], report: Report) -> None:
    """Checks that a `sync` conflict goes to a `rw` Stage, whose Stage boundary commit is the merge commit.

    Any other node leaves the merge unfinished in the Workspace, and the next `sync` cannot start its own.
    """
    for node in nodes.values():
        if not isinstance(node, Operation) or node.kind != "sync":
            continue
        target = node.next.get("conflict")
        stage = nodes.get(target) if target is not None else None
        if target in nodes and not (isinstance(stage, Stage) and stage.commits):
            report(
                f"operations.{node.name}",
                f"outcome conflict goes to {target}, "
                "but only a rw Stage can finish the merge that sync leaves in the Workspace",
            )


def _check_exhaustion(nodes: dict[str, Node], escalate_to: str | None, report: Report) -> None:
    """Checks that Stages out of Rounds never send a Ticket around a loop of Stages that are out of Rounds too.

    A route that resets Rounds may end such a loop, so the walk stops there.
    """
    for start in nodes.values():
        if not isinstance(start, Stage) or start.max_rounds is None:
            continue
        path = [start.name]
        current: Node = start
        while True:
            target = current.next.get("exhausted", escalate_to)
            following = nodes.get(target) if target is not None else None
            if current.resets.get("exhausted"):
                break
            if not isinstance(following, Stage) or following.max_rounds is None or following.name in path[1:]:
                break
            path.append(following.name)
            if following is start:
                report(f"stages.{start.name}", f"running out of rounds loops through {', '.join(path)}")
                break
            current = following


def _check_inputs(nodes: dict[str, Node], graph: "_Graph", report: Report) -> None:
    """Checks that every required input has an artifact on each path from the start to its node."""
    without_cause = graph.entered_without_cause()
    for node in nodes.values():
        where = f"{node.KIND}s.{node.name}"
        for item in node.inputs:
            if item.source == "cause":
                if node.name == graph.start:
                    report(where, "input cause has no source at the start node")
                elif node.name in without_cause:
                    report(where, "input cause can be missing on some path")
                continue
            if item.source in BUILTIN_INPUTS:
                continue
            undeclared = [name for name in item.nodes if name not in nodes or item.source != nodes[name].KIND]
            if undeclared:
                report(where, f"input {item.source} {' or '.join(undeclared)} is not declared")
            elif not item.optional and graph.reaches(node.name, without=set(item.nodes)):
                report(where, f"required input {item} can be missing on some path")


def _check_sources(nodes: dict[str, Node], graph: "_Graph", report: Report) -> None:
    """Checks that every path to a Gate with `from` passes through its source Gate, so it never reads a stale Probe."""
    for node in nodes.values():
        if not isinstance(node, Gate) or node.from_ is None:
            continue
        source = nodes.get(node.from_)
        if not isinstance(source, Gate) or source.from_ is not None:
            report(f"gates.{node.name}.from", f"{node.from_} is not a Gate that runs a Probe")
        elif graph.reaches(node.name, without={node.from_}):
            report(f"gates.{node.name}.from", f"a path reaches {node.name} without passing through {node.from_}")


def _check_resets(nodes: dict[str, Node], graph: "_Graph", report: Report) -> None:
    """Checks that a reset of Rounds never sits on a loop that runs with no Decision in it.

    Such a loop would start the round count over each time around, so the limit would never end it.
    """
    for node in nodes.values():
        if isinstance(node, Decision):
            continue
        for outcome, names in node.resets.items():
            for name in names:
                stage = nodes.get(name)
                if isinstance(stage, Stage) and stage.max_rounds is not None and graph.leads_to(name, node.name):
                    report(
                        f"{node.KIND}s.{node.name}.next.{outcome}",
                        f"reset_rounds names {name}, which leads back here with no Decision in between",
                    )


class _Graph:
    """_Graph splits each node into its entry and its artifact, so a path can be walked around a node's artifact.

    Escalating outcomes, a Gate's lost Probe and the Operation outcomes without an artifact leave from the entry,
    and the rest leave from the artifact.
    """

    def __init__(self, nodes: dict[str, Node], start: str, escalate_to: str | None) -> None:
        self.start = start
        self.nodes = set(nodes)
        self.decisions = {name for name, node in nodes.items() if isinstance(node, Decision)}
        self.edges: list[tuple[tuple[str, str], str, str]] = []
        for node in nodes.values():
            for outcome in node.outcomes:
                target = node.next.get(outcome)
                if target is None and outcome in ESCALATING_OUTCOMES:
                    target = escalate_to
                if target is not None:
                    entry = outcome in ESCALATING_OUTCOMES or (
                        isinstance(node, Operation) and outcome not in OPERATION_ARTIFACT_OUTCOMES
                    )
                    side = "entry" if entry else "artifact"
                    self.edges.append(((node.name, side), target, outcome))
            if isinstance(node, Gate) and escalate_to is not None:
                self.edges.append(((node.name, "entry"), escalate_to, "escalate"))

    def successors(self, name: str) -> list[str]:
        """Returns the nodes a Ticket can go to straight from `name`."""
        return [target for (source, _), target, _ in self.edges if source == name]

    def reaches(self, target: str, without: set[str]) -> bool:
        """Tells whether `target` can be entered from the start while no node in `without` writes an artifact."""
        return (target, "entry") in self._reachable(without)

    def entered_without_cause(self) -> set[str]:
        """Returns the nodes that can be entered with no artifact for `cause`.

        A Stage out of Rounds hands on the cause it was entered with, so `exhausted` carries a cause
        unless the Stage itself can be entered without one.
        """
        reachable = self._reachable(set())
        missing = {self.start}
        grew = True
        while grew:
            grew = False
            for (name, side), target, outcome in self.edges:
                if target in missing or (name, side) not in reachable or side != "entry":
                    continue
                if outcome != "exhausted" or name in missing:
                    missing.add(target)
                    grew = True
        return missing

    def leads_to(self, source: str, target: str) -> bool:
        """Tells whether a Ticket can go from `source` to `target` without passing through a Decision."""
        seen = set()
        todo = [source]
        while todo:
            current = todo.pop()
            if current == target:
                return True
            if current in seen or (current != source and current in self.decisions):
                continue
            seen.add(current)
            todo.extend(dst for (name, _), dst, _ in self.edges if name == current)
        return False

    def _reachable(self, without: set[str]) -> set[tuple[str, str]]:
        successors: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for src, dst, _ in self.edges:
            successors[src].add((dst, "entry"))
        for name in self.nodes - without:
            successors[(name, "entry")].add((name, "artifact"))
        seen: set[tuple[str, str]] = set()
        todo = [(self.start, "entry")]
        while todo:
            current = todo.pop()
            if current not in seen:
                seen.add(current)
                todo.extend(successors[current])
        return seen
