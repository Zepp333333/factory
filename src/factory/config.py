"""Config: reads the project file and the Pipeline it names into a model the Orchestrator walks."""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

TERMINAL_NODES = ("done", "abandoned")
OPERATION_KINDS = ("merge",)


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


@dataclass(frozen=True)
class Operation:
    name: str
    kind: str
    next: dict[str, str]


Node = Stage | Operation


@dataclass(frozen=True)
class Pipeline:
    path: Path
    start: str
    nodes: dict[str, Node]


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
        nodes[name] = Stage(
            name=name,
            role=roles[role_name],
            model=str(stage.get("model", defaults.get("model", "sonnet"))),
            inputs=[str(item) for item in stage.get("inputs", [])],
            next=dict(stage.get("next", {})),
        )
    for name, operation in data.get("operations", {}).items():
        kind = _require(operation, "kind", path)
        if kind not in OPERATION_KINDS:
            raise ConfigError(f"{path}: Operation {name} has unknown kind {kind}")
        nodes[name] = Operation(name=name, kind=kind, next=dict(operation.get("next", {})))
    start = _require(data, "start", path)
    if start not in nodes:
        raise ConfigError(f"{path}: start node {start} is not declared")
    return Pipeline(path=path, start=start, nodes=nodes)


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
