"""`factory diagram` prints a Pipeline or a Run graph as a Mermaid flowchart, and keeps it on top of the graph's file.

The diagram comes from the file alone, so `${vars.*}` stays as written and no project changes what a file's block says.
"""

import argparse
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from factory.cli.common import repo_of
from factory.config import ConfigError, display_path, read_yaml
from factory.pipeline.library_lookup import GRAPH_KINDS, Library
from factory.pipeline.pipeline import ESCALATING_OUTCOMES, OPERATION_OUTCOMES, RunNode
from factory.pipeline.run_kinds import OPERATION_SHAPE, TICKETS_SHAPE, WAIT_SHAPE
from factory.wiring import RUN_NODES, Machine

BEGIN = "# factory-diagram: begin, `factory diagram --write` writes this block"
END = "# factory-diagram: end"
BLOCK = re.compile(rf"^{re.escape(BEGIN)}\n(?:#.*\n)*?{re.escape(END)}\n\n?", re.MULTILINE)

STAGE = "stage"
GATE = "gate"
DECISION = "decision"
OPERATION = OPERATION_SHAPE
WAIT = WAIT_SHAPE
TERMINAL = "terminal"

# The brackets of each shape, as the diagrams of `docs/guide/` draw the four node kinds of a Pipeline.
SHAPES = {
    STAGE: ("[", "]"),
    GATE: ("{{", "}}"),
    DECISION: ("{", "}"),
    OPERATION: ("[/", "/]"),
    WAIT: ("((", "))"),
    TICKETS_SHAPE: ("[[", "]]"),
    TERMINAL: ("([", "])"),
}
PIPELINE_SECTIONS = {"stages": STAGE, "gates": GATE, "decisions": DECISION, "operations": OPERATION}

# The words that break a flowchart as the id of a node, which a node name may be.
MERMAID_WORDS = frozenset(
    ("call", "class", "classDef", "click", "default", "end", "flowchart", "graph", "href", "style", "subgraph")
)


def add_parser(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    diagram = commands.add_parser("diagram", help="print a Pipeline or a Run graph as a Mermaid flowchart")
    diagram.add_argument("target", metavar="name or file", help="a file, or a name that the Library resolves")
    diagram.add_argument("--kind", choices=tuple(GRAPH_KINDS), help="the kind of a name that is of both kinds")
    diagram.add_argument("--write", action="store_true", help="put the diagram on top of the file, as a comment block")
    diagram.add_argument("--project", type=Path, default=Path("factory.yaml"), help="project file")
    diagram.set_defaults(command=command)


def command(args: argparse.Namespace, machine: Machine) -> int:
    file = Path(args.target)
    if args.write:
        if not file.is_file():
            raise ConfigError(
                f"--write takes a file, and {args.target} is none;"
                " `factory library show` prints a graph to copy into one"
            )
        _write(file)
        print(f"{display_path(file)}: diagram written")
        return 0
    path = file if file.is_file() else _resolved(args.target, args.kind, machine.library(repo_of(args.project)))
    print(draw(_graph_of(path)))
    return 0


def _resolved(name: str, kind: str | None, library: Library) -> Path:
    """Returns the file of the graph that the Layers resolve `name` to, of `kind` when it says one."""
    found = {
        each: item.path
        for each, (directory, _) in GRAPH_KINDS.items()
        if kind in (None, each) and (item := library.find(directory, name)) is not None
    }
    if not found:
        raise ConfigError(f"{name} is no file and no Pipeline or Run graph of the Library")
    if len(found) > 1:
        raise ConfigError(
            f"{name} is both a Pipeline and a Run graph of the Library;"
            f" name one with {' or '.join(f'--kind {each}' for each in found)}"
        )
    return next(iter(found.values()))


def _graph_of(path: Path) -> dict[str, Any]:
    """Returns what a file that the owner named holds, which must look like a graph, however broken."""
    data = read_yaml(path)
    if not {"start", "nodes", *PIPELINE_SECTIONS} & data.keys():
        raise ConfigError(f"{display_path(path)} is no Pipeline and no Run graph")
    return data


def _write(path: Path) -> None:
    diagram = draw(_graph_of(path))
    text = with_block(path.read_text(), diagram)
    try:
        path.write_text(text)
    except OSError as error:
        raise ConfigError(f"cannot write {display_path(path)}: {error.strerror}") from error


def block_of(diagram: str) -> str:
    """Returns the comment block of a diagram, between the fixed markers."""
    lines = "".join(f"# {line}\n" for line in diagram.splitlines())
    return f"{BEGIN}\n{lines}{END}\n"


def with_block(text: str, diagram: str) -> str:
    """Returns the text of a graph's file with the block of `diagram` on top, in place of any block it held."""
    return f"{block_of(diagram)}\n{BLOCK.sub('', text)}"


def stale_blocks(library: Library, used: Iterable[Path]) -> list[str]:
    """Returns what is wrong with the block of each file in `used` and of each graph in a Layer of the Library."""
    files = list(used)
    for index, layer in enumerate(library.layers):
        for directory, _ in GRAPH_KINDS.values():
            files += [layer.root / directory / f"{name}.yaml" for name in library.names(directory, index)]
    problems = (_block_problem(path) for path in dict.fromkeys(file.resolve() for file in files))
    return [problem for problem in problems if problem is not None]


def _block_problem(path: Path) -> str | None:
    block = block_of(draw(read_yaml(path)))
    text = path.read_text()
    if text.startswith(block):
        return None
    shown = display_path(path)
    wrong = (
        "its diagram block differs from the diagram the file produces"
        if BEGIN in text
        else "has no diagram block at its top"
    )
    return f"{shown}: {wrong}; run `factory diagram --write {shown}`"


def draw(graph: dict[str, Any]) -> str:
    """Returns the Mermaid flowchart of a Pipeline or Run graph as the file says it, and tolerates a file that fails its
    check, because `factory check` reports what is wrong with one.
    """
    nodes = _nodes(graph)
    escalate_to = graph.get("escalate_to")
    start = "start" if escalate_to is None else f"start<br/>escalate_to: {_text(escalate_to)}"
    edges = [f"    _start --> {_id(str(graph.get('start')))}"]
    targets = []
    for name, (kind, body) in nodes.items():
        for _, target, label in _routes(body):
            edges.append(f'    {_id(name)} -->|"{_text(label)}"| {_id(target)}')
            targets.append(target)
        for outcome in _escalating(kind, body) if escalate_to is not None else ():
            edges.append(f'    {_id(name)} -.->|"{outcome}"| {_id(str(escalate_to))}')
            targets.append(str(escalate_to))
    if escalate_to is not None:
        targets.append(str(escalate_to))
    return "\n".join(
        (
            "flowchart TD",
            f"    _start{_shaped(TERMINAL, start)}",
            *(f"    {_id(name)}{_shaped(kind, _label(name, kind, body))}" for name, (kind, body) in nodes.items()),
            *(f"    {_id(t)}{_shaped(TERMINAL, _text(t))}" for t in dict.fromkeys(targets) if t not in nodes),
            *edges,
        )
    )


def _nodes(graph: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Returns the kind of shape and the body of each node, in the order of the file."""
    if "nodes" in graph:
        return {str(name): (_run_kind(body), body) for name, body in _bodies(graph["nodes"]).items()}
    return {
        str(name): (kind, body)
        for section, kind in PIPELINE_SECTIONS.items()
        for name, body in _bodies(graph.get(section)).items()
    }


def _bodies(section: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(section, dict):
        return {}
    return {name: body if isinstance(body, dict) else {} for name, body in section.items()}


def _run_kind(body: dict[str, Any]) -> str:
    """Returns the kind of shape of a Run node, told apart the way the loader tells it."""
    run_kind = RUN_NODES.get(str(body.get("kind")))
    if "role" in body or (run_kind is not None and run_kind.agent is not None):
        return STAGE
    if "decider" in body:
        return DECISION
    return run_kind.shape if run_kind is not None else GATE


def _routes(body: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Returns the outcome, the target and the label of each route the node declares, with `rounds.then` last."""
    routes = []
    next_ = body.get("next")
    for outcome, route in (next_ if isinstance(next_, dict) else {}).items():
        target = route.get("to") if isinstance(route, dict) else route
        resets = route.get("reset_rounds") if isinstance(route, dict) else None
        if target is not None:
            label = f"{outcome}, reset_rounds: {', '.join(map(str, resets))}" if isinstance(resets, list) else outcome
            routes.append((str(outcome), str(target), str(label)))
    rounds = body.get("rounds")
    if isinstance(rounds, dict) and "then" in rounds:
        routes.append(("exhausted", str(rounds["then"]), "exhausted"))
    return routes


def _escalating(kind: str, body: dict[str, Any]) -> list[str]:
    """Returns the escalating outcomes of a node that it leaves unrouted, which go to `escalate_to`."""
    routed = {outcome for outcome, _, _ in _routes(body)}
    return [outcome for outcome in _outcomes(kind, body) if outcome in ESCALATING_OUTCOMES and outcome not in routed]


def _outcomes(kind: str, body: dict[str, Any]) -> tuple[str, ...]:
    if kind == STAGE:
        return ("exhausted", "failed") if "rounds" in body else ("failed",)
    if kind == DECISION:
        return ("escalate",)
    run_kind = RUN_NODES.get(str(body.get("kind")))
    if run_kind is not None and kind == OPERATION:
        # The diagram tolerates a broken node, so what the parse rejects stays for `factory check` to report.
        settings = run_kind.parse("", body, lambda where, message: None)
        return run_kind.outcomes(RunNode(name="", spec=run_kind, next={}, settings=settings))
    if kind != OPERATION:
        return ()
    own = OPERATION_OUTCOMES.get(str(body.get("kind")), ())
    return (*own, "failed") if body.get("on_fail") == "escalate" else own


def _label(name: str, kind: str, body: dict[str, Any]) -> str:
    return "<br/>".join(_text(line) for line in (name, *_details(kind, body)))


def _details(kind: str, body: dict[str, Any]) -> list[str]:
    """Returns what the node shows below its name: its Role and round limit, its kind, or the Pipeline it runs."""
    if kind == STAGE:
        rounds = body.get("rounds")
        limit = [f"rounds: max {rounds.get('max')}"] if isinstance(rounds, dict) else []
        return [f"role: {body.get('role')}", *limit]
    if kind == DECISION:
        decider = body.get("decider")
        return [str(decider.get("kind"))] if isinstance(decider, dict) else []
    given = body.get("with")
    with_ = given if isinstance(given, dict) else {}
    if kind == TICKETS_SHAPE:
        pipeline = with_.get("pipeline")
        return [f"tickets: {pipeline.get('file') if isinstance(pipeline, dict) else pipeline}"]
    timeout = [f"timeout: {with_['timeout']}"] if "timeout" in with_ else []
    return [str(body.get("kind")), *timeout]


def _shaped(kind: str, label: str) -> str:
    opening, closing = SHAPES[kind]
    return f'{opening}"{label}"{closing}'


def _id(name: str) -> str:
    return f"{name}_" if name in MERMAID_WORDS else name


def _text(value: Any) -> str:
    """Returns text that stands inside the quotes of a label, where Mermaid reads `#` as the start of an entity."""
    return str(value).replace("#", "#35;").replace('"', "#quot;")
