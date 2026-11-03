"""Config reads the project file, and the Pipeline it names through `pipeline_loader`, into a checked Project.

Loading collects every error before it gives up, so `factory check` and `factory run` report them all at once.
Config knows the whole v1 format but refuses the parts this engine does not run yet, so a Run never meets them.
Each key of the project file resolves through its Sources, so the Factory config, the file and `--set` read alike.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

import yaml

from factory.core import spawn, values
from factory.pipeline import pipeline_checks
from factory.pipeline.library_lookup import Library
from factory.pipeline.pipeline import Pipeline
from factory.pipeline.throttle import Brake, parse_brake, parse_window
from factory.pipeline_loader import Checks, Errors, PipelineLoader, apply_overrides, read_yaml, substitute
from factory.pipeline_loader import ConfigError as ConfigError
from factory.pipeline_loader import display_path as display_path
from factory.ports.kind import Choice, Kind

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
MACHINE_FIELDS = ("slots", *PROJECT_FIELDS)

# Attempts that all Runs on a machine may have going at once, unless the factory config says otherwise.
DEFAULT_SLOTS = 4

# The Executor a project file gets when it names none.
DEFAULT_EXECUTOR = "claude-code"
DEFAULT_SANDBOX = "local"
DEFAULT_WORKBENCH = "terminal"
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# The built-in Source: what each key of the project file is when no other Source sets it, where None is unset.
BUILT_IN: dict[str, Any] = {
    "release": None,
    "max_parallel": 1,
    "throttle": {},
    "caches": [],
    "vars": {},
    "overrides": {},
    "sandbox": {},
    "workbench": DEFAULT_WORKBENCH,
    "executor": DEFAULT_EXECUTOR,
    "code_host": None,
    "prevent_sleep": True,
    "ci": {},
    "landing": "single",
}


@dataclass(frozen=True)
class Sources:
    """Sources are where the keys of the project file resolve, a later one winning over an earlier one:
    the built-in default, the keys `factory_config` that the Factory config sets for every project on the machine,
    the project file, and the keys `invocation` that `--set` gives.

    A Source merges into the blocks of the Sources below it, and a key it sets to None goes back to unset.
    `recorded` holds the settings a Run froze in `RunStarted`, and replaces each of its keys whole,
    so a resumed Run goes on as it began whatever the project file says by then.
    The Journal never holds the values of `sandbox.env`, so those still come from the Sources below.
    """

    factory_config: Mapping[str, Any] = dataclass_field(default_factory=dict)
    invocation: Mapping[str, Any] = dataclass_field(default_factory=dict)
    recorded: Mapping[str, Any] | None = None

    def resolve(self, file: Mapping[str, Any]) -> dict[str, Any]:
        """Returns the keys of the project file `file` as every Source resolves them."""
        data = merged(merged(merged(BUILT_IN, self.factory_config), file), self.invocation)
        if self.recorded is not None:
            env = _env_of(data.get("sandbox"))
            data.update(self.recorded)
            if env is not None and isinstance(data.get("sandbox"), Mapping):
                data["sandbox"] = {**data["sandbox"], "env": env}
        return {key: value for key, value in data.items() if value is not None}


def recordable(data: Mapping[str, Any]) -> dict[str, Any]:
    """Returns every key of the project file as `data` resolves it, without the values of `sandbox.env`."""
    settings = {key: data.get(key) for key in PROJECT_FIELDS}
    if isinstance(settings["sandbox"], Mapping):
        settings["sandbox"] = {key: value for key, value in settings["sandbox"].items() if key != "env"}
    return settings


def _env_of(sandbox: Any) -> Any:
    return sandbox.get("env") if isinstance(sandbox, Mapping) else None


def merged(below: Mapping[str, Any], above: Mapping[str, Any]) -> dict[str, Any]:
    """Returns `above` laid over `below`: blocks merge key by key, and any other value of `above` replaces."""
    result = dict(below)
    for key, value in above.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = merged(result[key], value)
        else:
            result[key] = value
    return result


def parse_set(text: str) -> dict[str, Any]:
    """Reads one `--set key=value` into a Source, where a dotted key reaches into blocks and the value is YAML."""
    key, sep, value = text.partition("=")
    if not sep or not key or any(not part for part in key.split(".")):
        raise values.Invalid("must be key=value, such as landing=stack or ci.timeout=30m")
    try:
        setting: Any = yaml.safe_load(value)
    except yaml.YAMLError as error:
        raise values.Invalid(f"{value} is not valid YAML: {error}") from error
    for part in reversed(key.split(".")):
        setting = {part: setting}
    return dict(setting)


@dataclass(frozen=True)
class Kinds:
    """Kinds are the kinds of the adapters a project file may pick by their names, as the registries of `wiring`
    hold them.

    `checks` holds the Checks of each Executor by its name.
    """

    executors: Mapping[str, Kind[Any]]
    sandboxes: Mapping[str, Kind[Any]]
    workbenches: Mapping[str, Kind[Any]]
    code_hosts: Mapping[str, Kind[Any]]
    checks: Mapping[str, Checks]


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
    sandbox: Choice
    sandbox_env: Mapping[str, str] = dataclass_field(default_factory=dict)
    max_parallel: int = 1
    throttle: dict[str, Brake] = dataclass_field(default_factory=dict)
    caches: frozenset[str] = frozenset()
    release: str | None = None
    workbench: Choice = Choice(DEFAULT_WORKBENCH)
    executor: Choice = Choice(DEFAULT_EXECUTOR)
    code_host: Choice | None = None
    prevent_sleep: bool = True
    ci: Ci | None = Ci()
    landing: str = "single"
    library: Library | None = None
    settings: dict[str, Any] = dataclass_field(default_factory=dict)
    unlike_file: tuple[str, ...] = ()


def load_project(
    path: Path, library_for: Callable[[Path], Library], kinds: Kinds, sources: Sources | None = None
) -> Project:
    """Loads the project file and its Pipeline through `sources`, or raises ConfigError with every error found.

    `library_for` returns the Library of the Target project at the given repository,
    and `kinds` lists the executors, Sandboxes, Workbenches and code hosts that the project file may name.
    The Project's `settings` are every key as its Sources resolved it, which `RunStarted` records,
    and `unlike_file` names the keys whose value is not what the project file alone gives.
    """
    path = path.resolve()
    file = read_yaml(path)
    sources = sources or Sources()
    data = sources.resolve(file)
    settings = recordable(data)
    alone = recordable(Sources().resolve(file))
    unlike_file = tuple(key for key in PROJECT_FIELDS if alone[key] != settings[key])
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
    # Every kind of Sandbox takes `env`, which every `exec` in the Sandbox carries.
    sandbox_env = _sandbox_env(data["sandbox"].get("env", {}), errors) if isinstance(data.get("sandbox"), dict) else {}
    sandbox = _choice("sandbox", data.get("sandbox", {}), errors, kinds.sandboxes, DEFAULT_SANDBOX, ("env",))
    max_parallel = errors.parsed(data, "max_parallel", values.positive, 1)
    throttle = _throttle(errors.mapping("throttle", data.get("throttle", {})), errors)
    caches: frozenset[str] = errors.parsed(data, "caches", _caches, frozenset())
    release: str | None = errors.parsed(data, "release", values.choice(RELEASES), None)
    workbench = _choice(
        "workbench", data.get("workbench", DEFAULT_WORKBENCH), errors, kinds.workbenches, DEFAULT_WORKBENCH
    )
    executor = _choice("executor", data.get("executor", DEFAULT_EXECUTOR), errors, kinds.executors, DEFAULT_EXECUTOR)
    code_host = _choice("code_host", data["code_host"], errors, kinds.code_hosts) if "code_host" in data else None
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
    pipeline = PipelineLoader(
        pipeline_errors, library, tuple(kinds.executors), kinds.checks[executor.kind], executor.kind
    ).load(raw)
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
        sandbox_env=sandbox_env,
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
        settings=settings,
        unlike_file=unlike_file,
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
    data = _machine_config(path)
    errors = Errors(path)
    slots = errors.parsed(data, "slots", values.positive, DEFAULT_SLOTS)
    if errors.messages:
        raise ConfigError(*errors.messages)
    return slots


def load_defaults(path: Path) -> dict[str, Any]:
    """Reads the keys of the project file that the factory config sets for every project on the machine."""
    return {key: value for key, value in _machine_config(path).items() if key in PROJECT_FIELDS}


def _machine_config(path: Path) -> dict[str, Any]:
    """Reads the factory config, which may be missing, and refuses the keys it does not know."""
    if not path.is_file():
        return {}
    data = read_yaml(path)
    errors = Errors(path)
    errors.fields("", data, MACHINE_FIELDS)
    if errors.messages:
        raise ConfigError(*errors.messages)
    return data


def _choice(
    port: str,
    value: Any,
    errors: Errors,
    kinds: Mapping[str, Kind[Any]],
    default: str = "",
    shared: tuple[str, ...] = (),
) -> Choice:
    """Reads the block of a port, `{kind: <name>, ...}` or only the kind's name: `kind` here, the fields `shared` by
    every kind to the caller, and the other fields by the parser of that kind.

    A block without `kind` picks `default`, and a kind that is not registered gives the Choice of `default`.
    """
    names = tuple(kinds)
    if isinstance(value, dict):
        body = value
    elif isinstance(value, str) and value in kinds:
        body = {"kind": value}
    else:
        errors.add(port, f"must be one of {', '.join(names)}")
        return Choice(default)
    errors.fields(port, body, ("kind", *shared, *dict.fromkeys(f for kind in kinds.values() for f in kind.fields)))
    kind = body.get("kind", default)
    if not isinstance(kind, str) or kind not in kinds:
        one_of = f"must be {' or '.join(names)}" if len(names) < 3 else f"must be one of {', '.join(names)}"
        errors.add(f"{port}.kind", one_of)
        return Choice(default)
    own = kinds[kind]
    for key in sorted(body.keys() - {"kind", *shared}):
        owners = [name for name, other in kinds.items() if key in other.fields]
        if key not in own.fields and owners:
            errors.add(f"{port}.{key}", f"applies only to kind {' or '.join(owners)}")
    if "egress" in body and "egress" in own.fields and not own.enforces_egress:
        errors.add(f"{port}.egress", f"cannot be enforced by kind {kind}")
    known = {key: field for key, field in body.items() if key in own.fields}
    return Choice(kind, own.parse(known, lambda key, message: errors.add(f"{port}.{key}", message)))


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
