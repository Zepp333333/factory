"""Config reads the project file, and the Pipeline it names through `pipeline_loader`, into a checked Project.

Loading collects every error before it gives up, so `factory check` and `factory run` report them all at once.
Config knows the whole v1 format but refuses the parts this engine does not run yet, so a Run never meets them.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from factory import pipeline_checks, spawn, values
from factory.library_lookup import Library
from factory.pipeline import Pipeline
from factory.pipeline_loader import ConfigError as ConfigError
from factory.pipeline_loader import Errors, Kinds, PipelineLoader, apply_overrides, read_yaml, substitute
from factory.pipeline_loader import display_path as display_path
from factory.sandbox import LocalSettings, SandboxChoice, SandboxKind
from factory.throttle import Brake, parse_brake, parse_window

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
    "executor",
    "code_host",
    "prevent_sleep",
    "ci",
    "landing",
)
CI_FIELDS = ("timeout", "grace")
RELEASES = ("redeploy", "shipped_binary")
LANDINGS = ("single", "stack")
MACHINE_FIELDS = ("slots",)

# Attempts that all Runs on a machine may have going at once, unless the factory config says otherwise.
DEFAULT_SLOTS = 4

# The Executor a project file gets when it names none.
DEFAULT_EXECUTOR = "claude-code"
DEFAULT_SANDBOX = "local"
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Ci:
    """Ci is how long a Run waits for the CI of its final PR: for the whole CI to end, and for a first check to show.

    A Run past `grace` seconds without any check on the PR's head commit takes the project as one without CI.
    """

    timeout: float = 3600.0
    grace: float = 180.0


@dataclass(frozen=True)
class Project:
    path: Path
    repo: Path
    base: str
    pipeline: Pipeline
    sandbox: SandboxChoice
    max_parallel: int = 1
    throttle: dict[str, Brake] = dataclass_field(default_factory=dict)
    caches: frozenset[str] = frozenset()
    release: str | None = None
    workbench: str = "terminal"
    executor: str = DEFAULT_EXECUTOR
    code_host: str | None = None
    prevent_sleep: bool = True
    ci: Ci | None = Ci()
    landing: str = "single"
    library: Library | None = None


def load_project(path: Path, library_for: Callable[[Path], Library], kinds: Kinds) -> Project:
    """Loads the project file and its Pipeline, or raises ConfigError with every error found.

    `library_for` returns the Library of the Target project at the given repository,
    and `kinds` lists the executors, Sandboxes, Workbenches and code hosts that the project file may name.
    """
    path = path.resolve()
    data = read_yaml(path)
    errors = Errors(path)
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
    sandbox = _sandbox(errors.mapping("sandbox", data.get("sandbox", {})), errors, kinds.sandboxes)
    max_parallel = errors.parsed(data, "max_parallel", values.positive, 1)
    throttle = _throttle(errors.mapping("throttle", data.get("throttle", {})), errors)
    caches: frozenset[str] = errors.parsed(data, "caches", _caches, frozenset())
    release: str | None = errors.parsed(data, "release", values.choice(RELEASES), None)
    workbench: str = errors.parsed(data, "workbench", values.choice(kinds.workbenches), "terminal")
    executor: str = errors.parsed(data, "executor", values.choice(kinds.executors), DEFAULT_EXECUTOR)
    code_host: str | None = errors.parsed(data, "code_host", values.choice(kinds.code_hosts), None)
    prevent_sleep: bool = errors.parsed(data, "prevent_sleep", values.boolean, True)
    ci = _ci(data.get("ci", {}), errors)
    landing: str = errors.parsed(data, "landing", values.choice(LANDINGS), "single")
    before = len(errors.messages)
    overrides = substitute(errors.mapping("overrides", data.get("overrides", {})), variables, "overrides", errors)
    pipeline_errors = errors.at(pipeline_path)
    try:
        raw = substitute(read_yaml(pipeline_path), variables, "", pipeline_errors)
    except ConfigError as error:
        raise ConfigError(*errors.messages, *error.errors) from error
    unknown_variables = len(errors.messages) > before
    apply_overrides(raw, overrides, errors)

    # A Pipeline with unknown variables would only add errors about the placeholders left in it.
    if unknown_variables:
        raise ConfigError(*errors.messages)
    pipeline = PipelineLoader(pipeline_errors, library, kinds, executor).load(raw)
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
        executor=executor,
        code_host=code_host,
        prevent_sleep=prevent_sleep,
        ci=ci,
        landing=landing,
        library=library,
    )


def _ci(body: Any, errors: Errors) -> Ci | None:
    """Reads `ci`, which is a block of `timeout` and `grace`, or `off` (YAML reads it as false) for no wait."""
    if body is False or body == "off":
        return None
    if not isinstance(body, dict):
        errors.add("ci", "must be a mapping, or off")
        return Ci()
    errors.fields("ci", body, CI_FIELDS)
    defaults = Ci()
    return Ci(
        timeout=_duration_of(body, "timeout", defaults.timeout, errors),
        grace=_duration_of(body, "grace", defaults.grace, errors),
    )


def _duration_of(body: dict[str, Any], key: str, default: float, errors: Errors) -> float:
    if key not in body:
        return default
    try:
        return values.duration(body[key])
    except values.Invalid as error:
        errors.add(f"ci.{key}", str(error))
        return default


def _pipeline_path(value: Any, base_dir: Path, library: Library, errors: Errors) -> Path | None:
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


def _throttle(body: dict[str, Any], errors: Errors) -> dict[str, Brake]:
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
    data = read_yaml(path)
    errors = Errors(path)
    errors.fields("", data, MACHINE_FIELDS)
    slots = errors.parsed(data, "slots", values.positive, DEFAULT_SLOTS)
    if errors.messages:
        raise ConfigError(*errors.messages)
    return slots


def _sandbox(body: dict[str, Any], errors: Errors, kinds: Mapping[str, SandboxKind]) -> SandboxChoice:
    """Reads the `sandbox` block: `kind` here, and the other fields by the parser of that kind."""
    names = tuple(kinds)
    errors.fields("sandbox", body, ("kind", "env", *dict.fromkeys(f for kind in kinds.values() for f in kind.fields)))
    env = _sandbox_env(body.get("env", {}), errors)
    kind = body.get("kind", DEFAULT_SANDBOX)
    if kind not in kinds:
        errors.add(
            "sandbox.kind", f"must be {' or '.join(names)}" if len(names) < 3 else f"must be one of {', '.join(names)}"
        )
        return SandboxChoice(DEFAULT_SANDBOX, LocalSettings(), env)
    own = kinds[kind]
    for key in sorted(body.keys() - {"kind", "env"}):
        owners = [name for name, other in kinds.items() if key in other.fields]
        if key not in own.fields and owners:
            errors.add(f"sandbox.{key}", f"applies only to kind {' or '.join(owners)}")
    if "egress" in body and "egress" in own.fields and not own.enforces_egress:
        errors.add("sandbox.egress", f"cannot be enforced by kind {kind}")
    known = {key: value for key, value in body.items() if key in own.fields}
    return SandboxChoice(kind, own.parse(known, lambda key, message: errors.add(f"sandbox.{key}", message)), env)


def _sandbox_env(body: Any, errors: Errors) -> dict[str, str]:
    """Reads `sandbox.env`, a mapping of variable names to strings, and refuses the names the factory sets itself."""
    found = errors.mapping("sandbox.env", body)
    env: dict[str, str] = {}
    for name, value in found.items():
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name):
            errors.add("sandbox.env", f"{name!r} is not a variable name")
        elif name.startswith("FACTORY_"):
            errors.add(f"sandbox.env.{name}", "names are reserved for the factory")
        elif not isinstance(value, str):
            errors.add(f"sandbox.env.{name}", "must be a string")
        else:
            env[name] = value
    return env


def _repo_of(path: Path, errors: Errors) -> Path:
    proc = spawn.run(["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        errors.at(path).add("", "is not inside a git repository")
    return Path(proc.stdout.strip())
