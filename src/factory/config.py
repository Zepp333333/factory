"""Config reads the project file and the Pipeline it names into a model the Orchestrator walks.

Loading refuses a Pipeline whose routes lead nowhere, so a Run never meets an undeclared node.
The full pre-Run check lives in `factory check`.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

TERMINAL_NODES = ("done", "abandoned")
STAGE_OUTCOMES = ("done", "exhausted", "failed")
OPERATION_OUTCOMES = {"merge": ("merged",)}
BRIEF_INPUTS = ("spec", "ticket")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Role:
    name: str
    prompt: Path
    output: Path


@dataclass(frozen=True)
class Stage:
    name: str
    role: Role
    model: str
    inputs: list[str]
    next: dict[str, str]

    @property
    def outcomes(self) -> tuple[str, ...]:
        return STAGE_OUTCOMES


@dataclass(frozen=True)
class Operation:
    name: str
    kind: str
    next: dict[str, str]

    @property
    def outcomes(self) -> tuple[str, ...]:
        return OPERATION_OUTCOMES[self.kind]


Node = Stage | Operation


@dataclass(frozen=True)
class Pipeline:
    path: Path
    start: str
    nodes: dict[str, Node]
    escalate_to: str | None

    def route(self, node: Node, outcome: str) -> str:
        """Returns the node an outcome leads to, sending unmapped outcomes to `escalate_to`."""
        target = node.next.get(outcome, self.escalate_to)
        assert target is not None, f"{node.name} has no route for {outcome}"
        return target


@dataclass(frozen=True)
class Project:
    path: Path
    repo: Path
    base: str
    pipeline: Pipeline


def load_project(path: Path) -> Project:
    path = path.resolve()
    data = _read_yaml(path)
    base = _require(data, "base", path)
    pipeline_path = (path.parent / _require(data, "pipeline", path)).resolve()
    return Project(path=path, repo=_repo_of(path), base=str(base), pipeline=load_pipeline(pipeline_path))


def load_pipeline(path: Path) -> Pipeline:
    data = _read_yaml(path)
    defaults = data.get("defaults", {})
    roles = {
        name: Role(
            name=name,
            prompt=path.parent / _require(role, "prompt", path),
            output=path.parent / _require(role, "output", path),
        )
        for name, role in data.get("roles", {}).items()
    }
    nodes: dict[str, Node] = {}
    for name, stage in data.get("stages", {}).items():
        role_name = _require(stage, "role", path)
        if role_name not in roles:
            raise ConfigError(f"{path}: Stage {name} names unknown Role {role_name}")
        workspace = stage.get("workspace", defaults.get("workspace", "rw"))
        if workspace != "rw":
            raise ConfigError(f"{path}: Stage {name} has workspace {workspace}, and only rw is supported yet")
        for source in stage.get("inputs", []):
            if source not in BRIEF_INPUTS:
                raise ConfigError(f"{path}: Stage {name} declares unknown input {source}")
        nodes[name] = Stage(
            name=name,
            role=roles[role_name],
            model=str(stage.get("model", defaults.get("model", "sonnet"))),
            inputs=[str(item) for item in stage.get("inputs", [])],
            next=dict(stage.get("next", {})),
        )
    for name, operation in data.get("operations", {}).items():
        kind = _require(operation, "kind", path)
        if kind not in OPERATION_OUTCOMES:
            raise ConfigError(f"{path}: Operation {name} has unknown kind {kind}")
        nodes[name] = Operation(name=name, kind=kind, next=dict(operation.get("next", {})))
    start = _require(data, "start", path)
    if start not in nodes:
        raise ConfigError(f"{path}: start node {start} is not declared")
    escalate_to = data.get("escalate_to")
    _check_routes(path, nodes, escalate_to)
    return Pipeline(path=path, start=start, nodes=nodes, escalate_to=escalate_to)


def _check_routes(path: Path, nodes: dict[str, Node], escalate_to: str | None) -> None:
    known = set(nodes) | set(TERMINAL_NODES)
    if escalate_to is not None and escalate_to not in known:
        raise ConfigError(f"{path}: escalate_to names undeclared node {escalate_to}")
    for node in nodes.values():
        for outcome, target in node.next.items():
            if outcome not in node.outcomes:
                raise ConfigError(f"{path}: {node.name} has no outcome {outcome}")
            if target not in known:
                raise ConfigError(f"{path}: {node.name} routes {outcome} to undeclared node {target}")
        if escalate_to is None:
            for outcome in node.outcomes:
                if outcome not in node.next:
                    raise ConfigError(f"{path}: {node.name} has no route for {outcome} and no escalate_to")


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error.strerror}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a YAML mapping")
    return data


def _require(data: dict[str, Any], key: str, path: Path) -> Any:
    if key not in data:
        raise ConfigError(f"{path}: missing `{key}`")
    return data[key]


def _repo_of(path: Path) -> Path:
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path.parent, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ConfigError(f"{path} is not inside a git repository")
    return Path(proc.stdout.strip())
