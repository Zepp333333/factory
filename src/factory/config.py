"""Config reads the project file and the Pipeline it names into a checked model the Orchestrator walks.

Loading collects every error before it gives up, so `factory check` and `factory run` report them all at once.
Config knows the whole v1 format but refuses the parts this engine does not run yet, so a Run never meets them.
"""

import copy
import json
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import jsonschema
import yaml

from factory import gates

TERMINAL_NODES = ("done", "abandoned")
STAGE_OUTCOMES = ("done", "exhausted", "failed")
GATE_OUTCOMES = ("pass", "fail")
OPERATION_OUTCOMES = {"merge": ("merged",), "sync": ("clean", "conflict")}

# Unmapped escalating outcomes go to `escalate_to`, and none of them leaves an artifact.
ESCALATING_OUTCOMES = ("exhausted", "failed", "escalate")

GATE_KINDS = ("command", "tests", "coverage", "diff-limits", "document")
DECIDER_KINDS = ("function", "model", "human")
BUILTIN_INPUTS = ("spec", "ticket", "cause")
REF_INPUTS = ("stage", "gate", "decision")

PROJECT_FIELDS = ("pipeline", "base", "vars", "overrides", "sandbox")
PIPELINE_FIELDS = ("start", "escalate_to", "defaults", "roles", "stages", "gates", "decisions", "operations")
DEFAULTS_FIELDS = ("model", "profile", "workspace", "executor", "attempts", "timeout")
ROLE_FIELDS = ("prompt", "output", "skills")
STAGE_FIELDS = ("role", *DEFAULTS_FIELDS, "inputs", "next", "rounds", "escalate")
GATE_FIELDS = ("kind", "with", "attempts", "clean", "next")
DECISION_FIELDS = ("decider", "outcomes", "inputs", "next")
DECIDER_FIELDS = ("kind", "role", "model")
OPERATION_FIELDS = ("kind", "next")
INPUT_FIELDS = (*REF_INPUTS, "optional", "rounds")

# These parts of the format are known, but this engine does not run them yet.
PENDING_FIELDS = ("sandbox", "profile", "executor", "skills", "rounds", "escalate")
RUNNABLE_GATE_KINDS = tuple(gates.KINDS)
RUNNABLE_DECIDER_KINDS: tuple[str, ...] = ()
RUNNABLE_OPERATION_KINDS = ("merge",)
RUNNABLE_INPUTS = ("spec", "ticket")

VAR = re.compile(r"\$\{vars\.(\w+)\}")


class ConfigError(Exception):
    def __init__(self, *errors: str) -> None:
        super().__init__("\n".join(errors))
        self.errors = list(errors)


@dataclass(frozen=True)
class Role:
    name: str
    prompt: Path
    output: Path


@dataclass(frozen=True)
class Input:
    """An Input is a builtin source such as `spec`, or a reference to the latest artifact of the named nodes."""

    source: str
    nodes: tuple[str, ...] = ()
    optional: bool = False
    all_rounds: bool = False

    def __str__(self) -> str:
        return " ".join([self.source, " or ".join(self.nodes)]).strip()


@dataclass(frozen=True)
class Stage:
    KIND: ClassVar[str] = "stage"
    name: str
    role: Role
    model: str
    inputs: tuple[Input, ...]
    next: dict[str, str]
    attempts: int
    timeout: float
    commits: bool

    @property
    def outcomes(self) -> tuple[str, ...]:
        return STAGE_OUTCOMES


@dataclass(frozen=True)
class Gate:
    """A Gate holds its `with` resolved against its kind, with defaults filled in."""

    KIND: ClassVar[str] = "gate"
    name: str
    kind: str
    with_: dict[str, Any]
    attempts: int
    clean: bool
    next: dict[str, str]
    inputs: tuple[Input, ...] = ()

    @property
    def outcomes(self) -> tuple[str, ...]:
        return GATE_OUTCOMES


@dataclass(frozen=True)
class Decider:
    kind: str
    role: Role | None
    model: str | None


@dataclass(frozen=True)
class Decision:
    KIND: ClassVar[str] = "decision"
    name: str
    decider: Decider
    declared: tuple[str, ...]
    inputs: tuple[Input, ...]
    next: dict[str, str]

    @property
    def outcomes(self) -> tuple[str, ...]:
        return (*self.declared, "escalate")


@dataclass(frozen=True)
class Operation:
    KIND: ClassVar[str] = "operation"
    name: str
    kind: str
    next: dict[str, str]
    inputs: tuple[Input, ...] = ()

    @property
    def outcomes(self) -> tuple[str, ...]:
        return OPERATION_OUTCOMES.get(self.kind, ())


Node = Stage | Gate | Decision | Operation


@dataclass(frozen=True)
class _Settings:
    """_Settings holds the Stage fields that `defaults` can set for every Stage."""

    model: str
    commits: bool
    attempts: int
    timeout: float


BUILTIN_SETTINGS = _Settings(model="sonnet", commits=True, attempts=1, timeout=3600.0)


@dataclass(frozen=True)
class Pipeline:
    path: Path
    start: str
    nodes: dict[str, Node]
    escalate_to: str | None

    def route(self, node: Node, outcome: str) -> str:
        """Returns the node an outcome leads to, sending unmapped escalating outcomes to `escalate_to`."""
        target = node.next.get(outcome)
        if target is None and outcome in ESCALATING_OUTCOMES:
            target = self.escalate_to
        assert target is not None, f"{node.name} has no route for {outcome}"
        return target


@dataclass(frozen=True)
class Project:
    path: Path
    repo: Path
    base: str
    pipeline: Pipeline


def load_project(path: Path) -> Project:
    """Loads the project file and its Pipeline, or raises ConfigError with every error found."""
    path = path.resolve()
    data = _read_yaml(path)
    errors = _Errors(path)
    errors.fields("", data, PROJECT_FIELDS)
    if "pipeline" not in data:
        errors.add("", "missing pipeline")
        raise ConfigError(*errors.messages)
    pipeline_path = (path.parent / str(data["pipeline"])).resolve()
    if "base" not in data:
        errors.add("", "missing base")
    variables = errors.mapping("vars", data.get("vars", {}))
    before = len(errors.messages)
    overrides = _substitute(errors.mapping("overrides", data.get("overrides", {})), variables, "overrides", errors)
    pipeline_errors = errors.at(pipeline_path)
    try:
        raw = _substitute(_read_yaml(pipeline_path), variables, "", pipeline_errors)
    except ConfigError as error:
        raise ConfigError(*errors.messages, *error.errors) from error
    unknown_variables = len(errors.messages) > before
    _apply_overrides(raw, overrides, errors)

    # A Pipeline with unknown variables would only add errors about the placeholders left in it.
    if unknown_variables:
        raise ConfigError(*errors.messages)
    pipeline = _PipelineLoader(pipeline_errors).load(raw)
    repo = _repo_of(path, errors)
    if errors.messages:
        raise ConfigError(*errors.messages)
    return Project(path=path, repo=repo, base=str(data["base"]), pipeline=pipeline)


class _Errors:
    """_Errors collects messages that name the file and the place in it."""

    def __init__(self, path: Path, shared: list[str] | None = None) -> None:
        self.path = path
        self.messages = [] if shared is None else shared

    def at(self, path: Path) -> "_Errors":
        """Returns a collector for another file that adds to the same list."""
        return _Errors(path, self.messages)

    def add(self, where: str, message: str) -> None:
        shown = display_path(self.path)
        self.messages.append(f"{shown}: {where}: {message}" if where else f"{shown}: {message}")

    def pending(self, where: str, what: str) -> None:
        self.add(where, f"{what} is not supported yet")

    def fields(self, where: str, body: dict[str, Any], allowed: tuple[str, ...]) -> None:
        for key in body:
            if key not in allowed:
                self.add(_join(where, key), "unknown field")
            elif key in PENDING_FIELDS:
                self.pending(where, key)

    def mapping(self, where: str, value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        self.add(where, "must be a mapping")
        return {}


def _apply_overrides(pipeline: dict[str, Any], overrides: dict[str, Any], errors: _Errors) -> None:
    """Merges the project's overrides into the Pipeline's nodes, and only into their Stage, Gate and Decider fields."""
    allowed = {"stages": ("Stage", STAGE_FIELDS), "gates": ("Gate", GATE_FIELDS), "decisions": ("Decision", ())}
    for section, patches in overrides.items():
        where = f"overrides.{section}"
        if section not in allowed:
            errors.add(where, f"{section} cannot be overridden, only stages, gates and decisions")
            continue
        label, fields = allowed[section]
        for name, patch in errors.mapping(where, patches).items():
            at = f"{where}.{name}"
            nodes = pipeline.get(section, {})
            node = nodes.get(name) if isinstance(nodes, dict) else None
            if not isinstance(node, dict):
                errors.add(at, f"there is no such {label}")
                continue
            patch = errors.mapping(at, patch)
            if section == "decisions":
                decider = errors.mapping(f"{at}.decider", patch.get("decider", {}))
                for key in sorted(patch.keys() - {"decider"}):
                    errors.add(f"{at}.{key}", "only the Decider of a Decision can be overridden")
                patch = {"decider": _overridable(f"{at}.decider", decider, DECIDER_FIELDS, errors)}
            else:
                patch = _overridable(at, patch, fields, errors)
            _merge(node, patch)


def _overridable(where: str, patch: dict[str, Any], fields: tuple[str, ...], errors: _Errors) -> dict[str, Any]:
    kept = {}
    for key, value in patch.items():
        if key in ROLE_FIELDS:
            errors.add(f"{where}.{key}", "a Role field cannot be overridden")
        elif key not in fields:
            errors.add(f"{where}.{key}", "unknown field")
        else:
            kept[key] = value
    return kept


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> None:
    """Merges dictionaries recursively and replaces everything else, lists included, as a whole."""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)


def _substitute(value: Any, variables: dict[str, Any], where: str, errors: _Errors) -> Any:
    """Replaces `${vars.*}`; a string that is a single reference takes the variable's value with its type."""
    if isinstance(value, dict):
        return {key: _substitute(item, variables, _join(where, str(key)), errors) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, variables, f"{where}[{index}]", errors) for index, item in enumerate(value)]
    if not isinstance(value, str):
        return value

    def lookup(match: re.Match[str]) -> Any:
        name = match.group(1)
        if name not in variables:
            errors.add(where, f"unknown variable {name}")
            return match.group(0)
        return variables[name]

    whole = VAR.fullmatch(value)
    if whole:
        return lookup(whole)
    return VAR.sub(lambda match: str(lookup(match)), value)


class _PipelineLoader:
    def __init__(self, errors: _Errors) -> None:
        self.errors = errors
        self.base_dir = errors.path.parent

    def load(self, data: dict[str, Any]) -> Pipeline:
        errors = self.errors
        errors.fields("", data, PIPELINE_FIELDS)
        self.defaults = errors.mapping("defaults", data.get("defaults", {}))
        errors.fields("defaults", self.defaults, DEFAULTS_FIELDS)
        self.fallback = self._settings("defaults", self.defaults, BUILTIN_SETTINGS)
        self.roles = {name: self._role(name, body) for name, body in self._section(data, "roles").items()}

        nodes: dict[str, Node] = {}
        parsers: dict[str, Callable[[str, str, dict[str, Any]], Node]] = {
            "stages": self._stage,
            "gates": self._gate,
            "decisions": self._decision,
            "operations": self._operation,
        }
        for section, parse in parsers.items():
            for name, body in self._section(data, section).items():
                where = f"{section}.{name}"
                if name in nodes or name in TERMINAL_NODES:
                    errors.add(where, f"node name {name} is already taken")
                    continue
                nodes[name] = parse(where, name, errors.mapping(where, body))

        start = str(data.get("start", ""))
        if not start:
            errors.add("", "missing start")
        elif start not in nodes:
            errors.add("start", f"names undeclared node {start}")
        escalate_to = data.get("escalate_to")
        if escalate_to is not None and escalate_to not in nodes and escalate_to not in TERMINAL_NODES:
            errors.add("escalate_to", f"names undeclared node {escalate_to}")

        if escalate_to is None:
            for node in nodes.values():
                if isinstance(node, Gate):
                    errors.add(f"gates.{node.name}", "a lost Probe escalates, so the Pipeline needs escalate_to")
        self._check_routes(nodes, escalate_to)
        if start in nodes:
            self._check_inputs(nodes, _Graph(nodes, start, escalate_to))
        return Pipeline(path=errors.path, start=start, nodes=nodes, escalate_to=escalate_to)

    def _section(self, data: dict[str, Any], name: str) -> dict[str, Any]:
        return self.errors.mapping(name, data.get(name, {}))

    def _role(self, name: str, body: Any) -> Role:
        where = f"roles.{name}"
        body = self.errors.mapping(where, body)
        self.errors.fields(where, body, ROLE_FIELDS)
        prompt = self._file(where, body, "prompt")
        output = self._file(where, body, "output")
        if output.is_file():
            try:
                jsonschema.Draft202012Validator.check_schema(json.loads(output.read_text()))
            except json.JSONDecodeError as error:
                self.errors.add(f"{where}.output", f"not valid JSON: {error}")
            except jsonschema.SchemaError as error:
                self.errors.add(f"{where}.output", f"not a valid JSON Schema: {error.message}")
        return Role(name=name, prompt=prompt, output=output)

    def _file(self, where: str, body: dict[str, Any], key: str) -> Path:
        if key not in body:
            self.errors.add(where, f"missing {key}")
            return Path()
        path = self.base_dir / str(body[key])
        if not path.is_file():
            self.errors.add(f"{where}.{key}", f"cannot read {path}")
        return path

    def _role_ref(self, where: str, name: Any) -> Role:
        if name in self.roles:
            return self.roles[name]
        self.errors.add(where, f"unknown Role {name}")

        # The stand-in keeps the graph checks running, and the error already stops the Pipeline from loading.
        return Role(name=str(name), prompt=Path(), output=Path())

    def _stage(self, where: str, name: str, body: dict[str, Any]) -> Stage:
        self.errors.fields(where, body, STAGE_FIELDS)
        if "role" not in body:
            self.errors.add(where, "missing role")
        role = self._role_ref(where, body.get("role"))
        settings = self._settings(where, body, self.fallback)
        inputs = self._inputs(where, body.get("inputs", []))
        return Stage(
            name=name,
            role=role,
            model=settings.model,
            inputs=inputs,
            next=self._next(where, body),
            attempts=settings.attempts,
            timeout=settings.timeout,
            commits=settings.commits,
        )

    def _settings(self, where: str, body: dict[str, Any], fallback: "_Settings") -> "_Settings":
        """Reads the fields a Stage shares with `defaults`, and takes the ones the body leaves out from `fallback`."""
        commits = fallback.commits
        if "workspace" in body:
            if body["workspace"] not in ("rw", "ro"):
                self.errors.add(_join(where, "workspace"), "must be rw or ro")
            else:
                commits = body["workspace"] == "rw"
        attempts = self._attempts(where, body, fallback.attempts)
        timeout = fallback.timeout
        if "timeout" in body:
            try:
                timeout = gates.duration(body["timeout"])
            except gates.Invalid as error:
                self.errors.add(_join(where, "timeout"), str(error))
        model = str(body.get("model", fallback.model))
        return _Settings(model=model, commits=commits, attempts=attempts, timeout=timeout)

    def _attempts(self, where: str, body: dict[str, Any], fallback: int) -> int:
        attempts = body.get("attempts", fallback)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            self.errors.add(_join(where, "attempts"), "must be a positive integer")
            return fallback
        return attempts

    def _gate(self, where: str, name: str, body: dict[str, Any]) -> Gate:
        self.errors.fields(where, body, GATE_FIELDS)
        kind = self._kind(where, body, GATE_KINDS, RUNNABLE_GATE_KINDS)
        with_ = self.errors.mapping(f"{where}.with", body.get("with", {}))
        if kind in gates.KINDS:
            with_, errors = gates.resolve_with(kind, with_)
            for field, message in errors:
                self.errors.add(_join(f"{where}.with", field), message)
        attempts = self._attempts(where, body, self.fallback.attempts)
        clean = body.get("clean", False)
        if not isinstance(clean, bool):
            self.errors.add(f"{where}.clean", "must be true or false")
        return Gate(
            name=name,
            kind=kind,
            with_=with_,
            attempts=attempts,
            clean=clean is True,
            next=self._next(where, body),
        )

    def _decision(self, where: str, name: str, body: dict[str, Any]) -> Decision:
        self.errors.fields(where, body, DECISION_FIELDS)
        decider = self.errors.mapping(f"{where}.decider", body.get("decider", {}))
        self.errors.fields(f"{where}.decider", decider, DECIDER_FIELDS)
        if decider.get("kind") == "consilium":
            self.errors.add(where, "Decider kind consilium is not implemented in v1")
            kind = "consilium"
        else:
            kind = self._kind(where, decider, DECIDER_KINDS, RUNNABLE_DECIDER_KINDS, "Decider kind")
        role = self._role_ref(where, decider["role"]) if "role" in decider else None
        model = str(decider["model"]) if "model" in decider else None
        declared = body.get("outcomes", [])
        if not isinstance(declared, list) or not declared:
            self.errors.add(f"{where}.outcomes", "must be a non-empty list")
            declared = []
        for outcome in declared:
            if outcome in ESCALATING_OUTCOMES:
                self.errors.add(f"{where}.outcomes", f"{outcome} is reserved and must not be declared")
        return Decision(
            name=name,
            decider=Decider(kind=kind, role=role, model=model),
            declared=tuple(str(outcome) for outcome in declared if outcome not in ESCALATING_OUTCOMES),
            inputs=self._inputs(where, body.get("inputs", [])),
            next=self._next(where, body),
        )

    def _kind(
        self, where: str, body: dict[str, Any], known: tuple[str, ...], runnable: tuple[str, ...], label: str = "kind"
    ) -> str:
        kind = str(body.get("kind", ""))
        if kind not in known:
            self.errors.add(where, f"unknown {label} {kind}" if kind else f"missing {label}")
        elif kind not in runnable:
            self.errors.pending(where, f"{label} {kind}")
        return kind

    def _operation(self, where: str, name: str, body: dict[str, Any]) -> Operation:
        self.errors.fields(where, body, OPERATION_FIELDS)
        kind = self._kind(where, body, tuple(OPERATION_OUTCOMES), RUNNABLE_OPERATION_KINDS)
        return Operation(name=name, kind=kind, next=self._next(where, body))

    def _next(self, where: str, body: dict[str, Any]) -> dict[str, str]:
        routes = {}
        for outcome, target in self.errors.mapping(f"{where}.next", body.get("next", {})).items():
            if isinstance(target, dict):
                if "reset_rounds" in target:
                    self.errors.pending(f"{where}.next.{outcome}", "reset_rounds")
                target = target.get("to")
            if not isinstance(target, str):
                self.errors.add(f"{where}.next.{outcome}", "must name a node")
                continue
            routes[str(outcome)] = target
        return routes

    def _inputs(self, where: str, value: Any) -> tuple[Input, ...]:
        if not isinstance(value, list):
            self.errors.add(f"{where}.inputs", "must be a list")
            return ()
        inputs = []
        for item in value:
            parsed = self._input(where, item)
            if parsed is None:
                continue
            if parsed.source not in RUNNABLE_INPUTS:
                self.errors.pending(where, f"input {parsed}")
            inputs.append(parsed)
        return tuple(inputs)

    def _input(self, where: str, item: Any) -> Input | None:
        if isinstance(item, str):
            if item in BUILTIN_INPUTS:
                return Input(item)
            self.errors.add(where, f"unknown input {item}")
            return None
        refs = [key for key in item if key in REF_INPUTS] if isinstance(item, dict) else []
        if len(refs) != 1 or set(item) - set(INPUT_FIELDS):
            self.errors.add(where, f"input {item} must be a builtin or name one of stage, gate and decision")
            return None
        names = item[refs[0]]
        optional = item.get("optional", False)
        if not isinstance(optional, bool):
            self.errors.add(where, f"input {item} has optional {optional}, and only true and false exist")
        rounds = item.get("rounds", "last")
        if rounds not in ("last", "all"):
            self.errors.add(where, f"input {item} has rounds {rounds}, and only last and all exist")
        return Input(
            source=refs[0],
            nodes=tuple(str(name) for name in (names if isinstance(names, list) else [names])),
            optional=optional is True,
            all_rounds=rounds == "all",
        )

    def _check_routes(self, nodes: dict[str, Node], escalate_to: str | None) -> None:
        known = set(nodes) | set(TERMINAL_NODES)
        for node in nodes.values():
            where = f"{node.KIND}s.{node.name}"
            for outcome in node.outcomes:
                target = node.next.get(outcome)
                if target is None and not (outcome in ESCALATING_OUTCOMES and escalate_to is not None):
                    self.errors.add(where, f"outcome {outcome} has no route")
                elif target is not None and target not in known:
                    self.errors.add(where, f"outcome {outcome} routes to undeclared node {target}")
            for outcome in node.next:
                if outcome not in node.outcomes:
                    self.errors.add(where, f"there is no outcome {outcome}")

    def _check_inputs(self, nodes: dict[str, Node], graph: "_Graph") -> None:
        """Checks that every required input has an artifact on each path from the start to its node."""
        for node in nodes.values():
            where = f"{node.KIND}s.{node.name}"
            for item in node.inputs:
                if item.source == "cause":
                    if node.name == graph.start:
                        self.errors.add(where, "input cause has no source at the start node")
                    elif graph.entered_without_artifact(node.name):
                        self.errors.add(where, "input cause can be missing on some path")
                    continue
                if item.source in BUILTIN_INPUTS:
                    continue
                undeclared = [name for name in item.nodes if name not in nodes or item.source != nodes[name].KIND]
                if undeclared:
                    self.errors.add(where, f"input {item.source} {' or '.join(undeclared)} is not declared")
                elif not item.optional and graph.reaches(node.name, without=set(item.nodes)):
                    self.errors.add(where, f"required input {item} can be missing on some path")


class _Graph:
    """_Graph splits each node into its entry and its artifact, so a path can be walked around a node's artifact.

    Escalating outcomes and a Gate's lost Probe leave from the entry because they produce no artifact,
    and the rest leave from the artifact.
    """

    def __init__(self, nodes: dict[str, Node], start: str, escalate_to: str | None) -> None:
        self.start = start
        self.nodes = set(nodes)
        self.edges: list[tuple[tuple[str, str], str]] = []
        for node in nodes.values():
            for outcome in node.outcomes:
                target = node.next.get(outcome)
                if target is None and outcome in ESCALATING_OUTCOMES:
                    target = escalate_to
                if target is not None:
                    side = "entry" if outcome in ESCALATING_OUTCOMES else "artifact"
                    self.edges.append(((node.name, side), target))
            if isinstance(node, Gate) and escalate_to is not None:
                self.edges.append(((node.name, "entry"), escalate_to))

    def reaches(self, target: str, without: set[str]) -> bool:
        """Tells whether `target` can be entered from the start while no node in `without` writes an artifact."""
        return (target, "entry") in self._reachable(without)

    def entered_without_artifact(self, target: str) -> bool:
        reachable = self._reachable(set())
        return any(src in reachable and src[1] == "entry" and dst == target for src, dst in self.edges)

    def _reachable(self, without: set[str]) -> set[tuple[str, str]]:
        successors: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for src, dst in self.edges:
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


def display_path(path: Path) -> str:
    """Shows a path relative to the working directory when that is shorter."""
    relative = os.path.relpath(path)
    return relative if len(relative) < len(str(path)) else str(path)


def _join(where: str, key: str) -> str:
    return ".".join(part for part in (where, key) if part)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as error:
        raise ConfigError(f"cannot read {display_path(path)}: {error.strerror}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"{display_path(path)} is not valid YAML: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"{display_path(path)} must hold a YAML mapping")
    return data


def _repo_of(path: Path, errors: _Errors) -> Path:
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path.parent, capture_output=True, text=True)
    if proc.returncode != 0:
        errors.at(path).add("", "is not inside a git repository")
    return Path(proc.stdout.strip())
