"""Config reads the project file and the Pipeline it names into the checked model of `factory.pipeline`.

Loading collects every error before it gives up, so `factory check` and `factory run` report them all at once.
Config knows the whole v1 format but refuses the parts this engine does not run yet, so a Run never meets them.
"""

import copy
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, get_args

import jsonschema
import yaml

from factory import deciders, gates, pipeline_checks, spawn, values
from factory.executor import Profile
from factory.library_lookup import Library, LibraryError
from factory.pipeline import (
    BUILTIN_INPUTS,
    ESCALATING_OUTCOMES,
    OPERATION_OUTCOMES,
    TERMINAL_NODES,
    Decider,
    Decision,
    Describer,
    Escalate,
    Gate,
    Input,
    Node,
    Operation,
    Pipeline,
    Role,
    Stage,
)
from factory.sandbox import SandboxKind, SandboxSettings
from factory.throttle import Brake, parse_brake, parse_window

GATE_KINDS = ("command", "tests", "coverage", "diff-limits", "document")
DECIDER_KINDS = tuple(deciders.KINDS)
REF_INPUTS = ("stage", "gate", "decision")

PROJECT_FIELDS = (
    "pipeline",
    "base",
    "release",
    "max_parallel",
    "throttle",
    "caches",
    "vars",
    "overrides",
    "sandbox",
    "workbench",
    "prevent_sleep",
)
RELEASES = ("redeploy", "shipped_binary")
WORKBENCHES = ("terminal", "orca")
SANDBOX_FIELDS = ("kind", "template", "cpus", "memory", "egress")
SANDBOX_KINDS = get_args(SandboxKind)
PIPELINE_FIELDS = (
    "start",
    "escalate_to",
    "defaults",
    "describer",
    "stages",
    "gates",
    "decisions",
    "operations",
)
SETTINGS_FIELDS = ("model", "profile", "workspace", "executor", "attempts", "timeout", "stall")
DEFAULTS_FIELDS = (*SETTINGS_FIELDS, "sandbox_retries")
ROLE_FIELDS = ("prompt", "schema", "skills")
STAGE_FIELDS = ("role", *SETTINGS_FIELDS, "inputs", "next", "rounds", "escalate")
GATE_FIELDS = ("kind", "with", "clean", "next", "from")
DESCRIBER_FIELDS = ("role", "model", "profile", "attempts", "timeout", "stall")
DECISION_FIELDS = ("decider", "outcomes", "inputs", "next")
DECIDER_FIELDS = ("kind", "role", "model", "function", "with", "profile", "attempts", "timeout", "stall")
OPERATION_FIELDS = ("kind", "next")
INPUT_FIELDS = (*REF_INPUTS, "optional", "rounds")
ROUNDS_FIELDS = ("max", "then")
ESCALATE_FIELDS = ("from_round", "model")
MACHINE_FIELDS = ("slots",)

# Attempts that all Runs on a machine may have going at once, unless the factory config says otherwise.
DEFAULT_SLOTS = 4

# These parts of the format are known, but this engine does not run them yet.
PENDING_FIELDS = ("executor",)
PROFILES = get_args(Profile)
RUNNABLE_GATE_KINDS = tuple(gates.KINDS)
RUNNABLE_DECIDER_KINDS = tuple(name for name, kind in deciders.KINDS.items() if kind.runnable)
RUNNABLE_OPERATION_KINDS = ("sync", "merge")

VAR = re.compile(r"\$\{vars\.(\w+)\}")
MEMORY = re.compile(r"[1-9]\d*[mg]")


class ConfigError(Exception):
    def __init__(self, *errors: str) -> None:
        super().__init__("\n".join(errors))
        self.errors = list(errors)


@dataclass(frozen=True)
class _Settings:
    """_Settings holds the Stage fields that `defaults` can set for every Stage."""

    model: str
    profile: Profile
    commits: bool
    attempts: int
    timeout: float
    stall: float | None = None


BUILTIN_SETTINGS = _Settings(model="sonnet", profile="project", commits=True, attempts=1, timeout=3600.0)


@dataclass(frozen=True)
class Project:
    path: Path
    repo: Path
    base: str
    pipeline: Pipeline
    sandbox: SandboxSettings
    max_parallel: int = 1
    throttle: dict[str, Brake] = dataclass_field(default_factory=dict)
    caches: frozenset[str] = frozenset()
    release: str | None = None
    workbench: str = "terminal"
    prevent_sleep: bool = True


def load_project(path: Path, library_for: Callable[[Path], Library]) -> Project:
    """Loads the project file and its Pipeline, or raises ConfigError with every error found.

    `library_for` returns the Library of the Target project at the given repository.
    """
    path = path.resolve()
    data = _read_yaml(path)
    errors = _Errors(path)
    errors.fields("", data, PROJECT_FIELDS)
    if "pipeline" not in data:
        errors.add("", "missing pipeline")
        raise ConfigError(*errors.messages)
    repo = _repo_of(path, errors)
    if errors.messages:
        raise ConfigError(*errors.messages)
    library = library_for(repo)
    pipeline_path = _pipeline_path(data["pipeline"], path.parent, library, errors)
    if pipeline_path is None:
        raise ConfigError(*errors.messages)
    if "base" not in data:
        errors.add("", "missing base")
    variables = errors.mapping("vars", data.get("vars", {}))
    sandbox = _sandbox(errors.mapping("sandbox", data.get("sandbox", {})), errors)
    max_parallel = errors.parsed(data, "max_parallel", values.positive, 1)
    throttle = _throttle(errors.mapping("throttle", data.get("throttle", {})), errors)
    caches: frozenset[str] = errors.parsed(data, "caches", _caches, frozenset())
    release: str | None = errors.parsed(data, "release", values.choice(RELEASES), None)
    workbench: str = errors.parsed(data, "workbench", values.choice(WORKBENCHES), "terminal")
    prevent_sleep: bool = errors.parsed(data, "prevent_sleep", values.boolean, True)
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
    pipeline = _PipelineLoader(pipeline_errors, library).load(raw)
    if max_parallel > 1 and not errors.messages:
        for source, merge in pipeline_checks.merges_without_sync(pipeline.nodes, pipeline.start, pipeline.escalate_to):
            errors.add(
                "max_parallel",
                f"{max_parallel} Tickets at once need a sync before merge, but {source} reaches {merge} without one",
            )
    if errors.messages:
        raise ConfigError(*errors.messages)
    library.flush()
    return Project(
        path=path,
        repo=repo,
        base=str(data["base"]),
        pipeline=pipeline,
        sandbox=sandbox,
        max_parallel=max_parallel,
        throttle=throttle,
        caches=caches,
        release=release,
        workbench=workbench,
        prevent_sleep=prevent_sleep,
    )


def _pipeline_path(value: Any, base_dir: Path, library: Library, errors: "_Errors") -> Path | None:
    """Returns the file of the Pipeline that `pipeline` names, by name in the Library or by `{file: <path>}`."""
    if isinstance(value, dict):
        errors.fields("pipeline", value, ("file",))
        file = value.get("file")
        if not isinstance(file, str):
            errors.add("pipeline", "must name a `file`")
            return None
        return (base_dir / file).resolve()
    if not isinstance(value, str):
        errors.add("pipeline", "must be a Pipeline name or `{file: <path>}`")
        return None
    if "/" in value or value.endswith((".yaml", ".yml")):
        errors.add(
            "pipeline",
            f"is the path {value}; write `pipeline: {{file: {value}}}`,"
            " or put the file in `pipelines/` of a Layer of the Library and name it",
        )
        return None
    found = library.find("pipelines", value)
    if found is None:
        errors.add("pipeline", f"Pipeline {value} is in no Layer of the Library")
        return None
    return found.path.resolve()


def _throttle(body: dict[str, Any], errors: "_Errors") -> dict[str, Brake]:
    """Reads the `throttle` block, which sets a brake for each of the known windows."""
    brakes = {}
    for name, brake in body.items():
        where = f"throttle.{name}"
        try:
            brakes[parse_window(name)] = parse_brake(brake)
        except values.Invalid as error:
            errors.add(where, str(error))
    return brakes


def _caches(value: Any) -> frozenset[str]:
    """Reads `caches`, the names of the directories whose ignored files a Stage may leave in the Workspace."""
    if not isinstance(value, list) or not all(
        isinstance(name, str) and name and "/" not in name and name not in (".", "..") for name in value
    ):
        raise values.Invalid("must be a list of directory names such as .venv or __pycache__")
    return frozenset(value)


def load_slots(path: Path) -> int:
    """Reads the machine's slot cap from the factory config, which may be missing and then gives DEFAULT_SLOTS."""
    if not path.is_file():
        return DEFAULT_SLOTS
    data = _read_yaml(path)
    errors = _Errors(path)
    errors.fields("", data, MACHINE_FIELDS)
    slots = errors.parsed(data, "slots", values.positive, DEFAULT_SLOTS)
    if errors.messages:
        raise ConfigError(*errors.messages)
    return slots


def _sandbox(body: dict[str, Any], errors: "_Errors") -> SandboxSettings:
    """Reads the `sandbox` block, where every field but `kind` belongs to kind `sbx`."""
    errors.fields("sandbox", body, SANDBOX_FIELDS)
    kind = body.get("kind", "local")
    if kind not in SANDBOX_KINDS:
        errors.add("sandbox.kind", "must be local or sbx")
        return SandboxSettings()
    if kind == "local":
        for key in sorted(body.keys() - {"kind"}):
            errors.add(f"sandbox.{key}", "applies only to kind sbx")
        return SandboxSettings()
    template = body.get("template", "")
    if not isinstance(template, str):
        errors.add("sandbox.template", "must be a string")
        template = ""
    cpus = 0
    if "cpus" in body:
        try:
            cpus = values.positive(body["cpus"])
        except values.Invalid as error:
            errors.add("sandbox.cpus", str(error))
    memory = body.get("memory", "")
    if "memory" in body and not (isinstance(memory, str) and MEMORY.fullmatch(memory)):
        errors.add("sandbox.memory", "must be a size such as 512m or 8g")
        memory = ""
    egress = body.get("egress", [])
    if not isinstance(egress, list) or not all(isinstance(host, str) and host for host in egress):
        errors.add("sandbox.egress", "must be a list of hosts")
        egress = []
    return SandboxSettings(kind="sbx", template=template, cpus=cpus, memory=memory, egress=tuple(egress))


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

    def parsed[T](self, body: dict[str, Any], key: str, parse: Callable[[Any], T], default: T) -> T:
        """Returns the top-level `key` of `body` parsed, or `default` when the key is missing or does not parse."""
        if key not in body:
            return default
        try:
            return parse(body[key])
        except values.Invalid as error:
            self.add(key, str(error))
            return default

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
    def __init__(self, errors: _Errors, library: Library) -> None:
        self.errors = errors
        self.library = library
        self.roles: dict[str, Role] = {}

    def load(self, data: dict[str, Any]) -> Pipeline:
        errors = self.errors
        if "roles" in data:
            errors.add(
                "roles",
                "a Pipeline has no `roles:` section; move each Role to `roles/<name>.yaml` in a Layer of the Library,"
                " with `prompt` and `schema` naming files of `prompts/` and `schemas/` there",
            )
        errors.fields("", {k: v for k, v in data.items() if k != "roles"}, PIPELINE_FIELDS)
        self.defaults = errors.mapping("defaults", data.get("defaults", {}))
        errors.fields("defaults", self.defaults, DEFAULTS_FIELDS)
        self.fallback = self._settings("defaults", self.defaults, BUILTIN_SETTINGS)

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

        pipeline_checks.check(nodes, start, escalate_to, errors.add)
        describer = self._describer(data["describer"]) if "describer" in data else None
        pipeline_checks.check_roles(nodes, describer, errors.add)
        sandbox_retries = 2
        if "sandbox_retries" in self.defaults:
            parsed = self._parsed("defaults.sandbox_retries", self.defaults["sandbox_retries"], values.non_negative)
            sandbox_retries = sandbox_retries if parsed is None else parsed
        return Pipeline(
            path=errors.path,
            start=start,
            nodes=nodes,
            escalate_to=escalate_to,
            sandbox_retries=sandbox_retries,
            describer=describer,
        )

    def _describer(self, value: Any) -> Describer:
        """Reads the `describer` block, which names a Role and sets the same fields of a Stage as `defaults` does."""
        body = self._block("describer", value, DESCRIBER_FIELDS)
        if "role" not in body:
            self.errors.add("describer", "missing role")
        role = self._role_ref("describer", body.get("role"))
        settings = self._settings("describer", body, self.fallback)
        return Describer(
            role=role,
            model=settings.model,
            profile=settings.profile,
            attempts=settings.attempts,
            timeout=settings.timeout,
            stall=settings.stall,
        )

    def _section(self, data: dict[str, Any], name: str) -> dict[str, Any]:
        return self.errors.mapping(name, data.get(name, {}))

    def _role_ref(self, where: str, name: Any) -> Role:
        """Resolves a Role through the Library, with a stand-in for one that fails, which the error already reports."""
        if isinstance(name, str) and name in self.roles:
            return self.roles[name]
        stand_in = Role(name=str(name), prompt=Path(), output=Path())
        if not isinstance(name, str):
            self.errors.add(where, f"Role must be a name, not {name!r}")
            return stand_in
        try:
            found, prompt, output, schema = self.library.materialize(name)
            jsonschema.Draft202012Validator.check_schema(schema.document)
        except LibraryError as error:
            self.errors.add(where, str(error))
            return stand_in
        except jsonschema.SchemaError as error:
            self.errors.add(where, f"the schema of Role {name} is not a valid JSON Schema: {error.message}")
            return stand_in
        role = Role(
            name=name,
            prompt=prompt,
            output=output,
            skills=tuple(found.skills),
            reads=found.reads,
            schema=schema.document,
        )
        self.roles[name] = role
        return role

    def _stage(self, where: str, name: str, body: dict[str, Any]) -> Stage:
        self.errors.fields(where, body, STAGE_FIELDS)
        if "role" not in body:
            self.errors.add(where, "missing role")
        role = self._role_ref(where, body.get("role"))
        settings = self._settings(where, body, self.fallback)
        inputs = self._inputs(where, body.get("inputs", []))
        routes, resets = self._next(where, body)
        max_rounds = limit = None
        if "rounds" in body:
            rounds = self._block(f"{where}.rounds", body["rounds"], ROUNDS_FIELDS)
            limit = self._required(f"{where}.rounds", rounds, "max", values.positive)

            # A broken limit still gives the Stage its outcome `exhausted`, so no second error follows.
            max_rounds = limit or 1
            if "then" in rounds:
                if "exhausted" in routes:
                    self.errors.add(where, "outcome exhausted is routed by both rounds.then and next")
                elif not isinstance(rounds["then"], str):
                    self.errors.add(f"{where}.rounds.then", "must name a node")
                else:
                    routes["exhausted"] = rounds["then"]
        escalate = None
        if "escalate" in body:
            block = self._block(f"{where}.escalate", body["escalate"], ESCALATE_FIELDS)
            from_round = self._required(f"{where}.escalate", block, "from_round", values.positive)
            model = block.get("model")
            if model is None:
                self.errors.add(f"{where}.escalate", "missing model")
            elif not isinstance(model, str):
                self.errors.add(f"{where}.escalate.model", "must be a string")
            if from_round is not None and limit is not None and from_round > limit:
                self.errors.add(f"{where}.escalate.from_round", f"is past rounds.max {limit}")

            # Errors above already stop the Pipeline from loading, so the stand-in values never run.
            escalate = Escalate(from_round=from_round or 1, model=str(model))
        return Stage(
            name=name,
            role=role,
            model=settings.model,
            inputs=inputs,
            next=routes,
            resets=resets,
            attempts=settings.attempts,
            timeout=settings.timeout,
            stall=settings.stall,
            commits=settings.commits,
            profile=settings.profile,
            max_rounds=max_rounds,
            escalate=escalate,
        )

    def _block(self, where: str, value: Any, allowed: tuple[str, ...]) -> dict[str, Any]:
        block = self.errors.mapping(where, value)
        self.errors.fields(where, block, allowed)
        return block

    def _required[T](self, where: str, block: dict[str, Any], key: str, parse: Callable[[Any], T]) -> T | None:
        if key not in block:
            self.errors.add(where, f"missing {key}")
            return None
        return self._parsed(_join(where, key), block[key], parse)

    def _parsed[T](self, where: str, value: Any, parse: Callable[[Any], T]) -> T | None:
        try:
            return parse(value)
        except values.Invalid as error:
            self.errors.add(where, str(error))
            return None

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
            timeout = self._parsed(_join(where, "timeout"), body["timeout"], values.duration) or fallback.timeout
        stall = fallback.stall
        if "stall" in body:
            stall = self._parsed(_join(where, "stall"), body["stall"], values.duration) or fallback.stall
        # A Stage that declares neither value inherits a pair that `defaults` already checked.
        declared = where == "defaults" or "stall" in body or "timeout" in body
        if stall is not None and stall >= timeout and declared:
            self.errors.add(_join(where, "stall"), f"must be less than timeout {timeout:g}s")
        profile = fallback.profile
        if "profile" in body:
            if body["profile"] not in PROFILES:
                self.errors.add(_join(where, "profile"), "must be project or hermetic")
            else:
                profile = body["profile"]
        model = str(body.get("model", fallback.model))
        return _Settings(model=model, profile=profile, commits=commits, attempts=attempts, timeout=timeout, stall=stall)

    def _attempts(self, where: str, body: dict[str, Any], fallback: int) -> int:
        if "attempts" not in body:
            return fallback
        return self._parsed(_join(where, "attempts"), body["attempts"], values.positive) or fallback

    def _gate(self, where: str, name: str, body: dict[str, Any]) -> Gate:
        self.errors.fields(where, body, GATE_FIELDS)
        kind = self._kind(where, body, GATE_KINDS, RUNNABLE_GATE_KINDS)
        with_ = self.errors.mapping(f"{where}.with", body.get("with", {}))
        if kind in gates.KINDS:
            with_, errors = gates.resolve_with(kind, with_)
            for field, message in errors:
                self.errors.add(_join(f"{where}.with", field), message)
        clean = body.get("clean", False)
        if not isinstance(clean, bool):
            self.errors.add(f"{where}.clean", "must be true or false")
        from_ = self._from(where, body, kind, with_)
        routes, resets = self._next(where, body)
        return Gate(
            name=name,
            kind=kind,
            with_=with_,
            clean=clean is True,
            next=routes,
            resets=resets,
            from_=from_,
        )

    def _from(self, where: str, body: dict[str, Any], kind: str, with_: dict[str, Any]) -> str | None:
        """Reads `from`, the Gate whose Probe this Gate judges, and checks it against `run`."""
        from_ = body.get("from")
        reads_from = kind in gates.KINDS and gates.KINDS[kind].reads_from
        if from_ is not None and (not reads_from or not isinstance(from_, str)):
            self.errors.add(f"{where}.from", f"must name a Gate, and a Gate of kind {kind} has no from")
            return None
        if reads_from:
            if from_ is None and not with_.get("run"):
                self.errors.add(f"{where}.with", "missing run, or from to read another Gate's Probe")
            elif from_ is not None and with_.get("run"):
                self.errors.add(f"{where}.with", "run is the Probe's, and a Gate with from has none")
        return from_

    def _decision(self, where: str, name: str, body: dict[str, Any]) -> Decision:
        self.errors.fields(where, body, DECISION_FIELDS)
        decider = self.errors.mapping(f"{where}.decider", body.get("decider", {}))
        self.errors.fields(f"{where}.decider", decider, DECIDER_FIELDS)
        kind = str(decider.get("kind", ""))
        if kind in deciders.KINDS and not deciders.KINDS[kind].runnable:
            self.errors.add(where, f"Decider kind {kind} is not implemented in v1")
        else:
            kind = self._kind(where, decider, DECIDER_KINDS, RUNNABLE_DECIDER_KINDS, "Decider kind")
        settings = self._settings(f"{where}.decider", decider, self.fallback)
        role = self._role_ref(where, decider["role"]) if "role" in decider else None
        # An unknown kind is already an error, and a bare Kind keeps it from adding more.
        traits = deciders.KINDS.get(kind, deciders.Kind())
        if traits.needs_role and role is None:
            self.errors.add(f"{where}.decider", "a model Decider needs a role")
        declared = body.get("outcomes", [])
        descriptions: dict[str, str] = {}
        if isinstance(declared, dict) and declared:
            descriptions = {str(name): str(text or "") for name, text in declared.items()}
            declared = list(declared)
        elif not isinstance(declared, list) or not declared:
            self.errors.add(f"{where}.outcomes", "must be a non-empty list")
            declared = []
        for outcome in declared:
            if outcome in ESCALATING_OUTCOMES:
                self.errors.add(f"{where}.outcomes", f"{outcome} is reserved and must not be declared")
        function, with_ = self._function(where, traits, decider, declared)
        routes, resets = self._next(where, body)
        return Decision(
            name=name,
            decider=Decider(
                kind=kind,
                role=role,
                model=settings.model,
                function=function,
                with_=with_,
                profile=settings.profile,
                attempts=settings.attempts,
                timeout=settings.timeout,
                stall=settings.stall,
            ),
            declared=tuple(str(outcome) for outcome in declared if outcome not in ESCALATING_OUTCOMES),
            inputs=self._inputs(where, body.get("inputs", [])),
            next=routes,
            resets=resets,
            descriptions=descriptions,
        )

    def _function(
        self, where: str, traits: deciders.Kind, decider: dict[str, Any], declared: list[Any]
    ) -> tuple[str | None, dict[str, Any]]:
        """Reads the function of a `function` Decider and its `with`, which may name only declared outcomes."""
        at = f"{where}.decider"
        if not traits.takes_function:
            for key in ("function", "with"):
                if key in decider:
                    self.errors.add(f"{at}.{key}", "only a function Decider takes this")
            return None, {}
        name = decider.get("function")
        if not isinstance(name, str) or name not in deciders.FUNCTIONS:
            known = ", ".join(deciders.FUNCTIONS)
            self.errors.add(at, f"unknown function {name}" if name else f"missing function, one of {known}")
            return None, {}
        with_, errors = deciders.resolve_with(name, self.errors.mapping(f"{at}.with", decider.get("with", {})))
        for field, message in errors:
            self.errors.add(_join(f"{at}.with", field), message)
        if not errors:
            for outcome in deciders.outcomes_of(name, with_):
                if outcome not in declared:
                    self.errors.add(f"{at}.with", f"outcome {outcome} is not declared in outcomes")
        return name, with_

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
        routes, resets = self._next(where, body)

        # The Orchestrator advances only active Tickets, so a node after `merged` would never run.
        merged = routes.get("merged")
        if kind == "merge" and merged not in (None, "done"):
            self.errors.add(where, f"outcome merged routes to {merged}, and a merged Ticket can only go to done")
        return Operation(name=name, kind=kind, next=routes, resets=resets)

    def _next(self, where: str, body: dict[str, Any]) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
        """Reads the routes of `next` and the Stages whose Rounds each route resets."""
        routes, resets = {}, {}
        for outcome, target in self.errors.mapping(f"{where}.next", body.get("next", {})).items():
            at = f"{where}.next.{outcome}"
            if isinstance(target, dict):
                self.errors.fields(at, target, ("to", "reset_rounds"))
                names = target.get("reset_rounds", [])
                if not isinstance(names, list):
                    self.errors.add(f"{at}.reset_rounds", "must be a list of Stages")
                elif names:
                    resets[str(outcome)] = tuple(str(name) for name in names)
                target = target.get("to")
            if not isinstance(target, str):
                self.errors.add(at, "must name a node")
                continue
            routes[str(outcome)] = target
        return routes, resets

    def _inputs(self, where: str, value: Any) -> tuple[Input, ...]:
        if not isinstance(value, list):
            self.errors.add(f"{where}.inputs", "must be a list")
            return ()
        inputs = []
        for item in value:
            parsed = self._input(where, item)
            if parsed is not None:
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
    proc = spawn.run(["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        errors.at(path).add("", "is not inside a git repository")
    return Path(proc.stdout.strip())
