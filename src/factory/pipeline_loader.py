"""The Pipeline loader reads a Pipeline file into the checked model of `factory.pipeline`.

It holds what reading the project file shares with it: the collector of errors, the names of the adapters a project
may pick and the substitution of variables and overrides that the project file applies to the Pipeline.
Loading collects every error before it gives up, and refuses the parts of the v1 format this engine does not run yet.
"""

import copy
import dataclasses
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, get_args

import jsonschema
import yaml

from factory.core import values
from factory.core.run_agent import CI_FIXER, DESCRIBER, RunAgent
from factory.core.states import RUN_TERMINAL_NODES, TERMINAL_NODES
from factory.pipeline import deciders, gates, pipeline_checks
from factory.pipeline.library_lookup import Library, LibraryError
from factory.pipeline.pipeline import (
    ASSIGN,
    BUILTIN_INPUTS,
    COMMENT,
    COMMENT_REFERENCES,
    ESCALATING_OUTCOMES,
    FILTER,
    LABEL,
    ON_FAIL,
    OPEN_PR,
    OPERATION_OUTCOMES,
    RUN_NODE_KINDS,
    SPEC_TARGET,
    TRACKER_OPERATIONS,
    WARN,
    AgentSettings,
    Decider,
    Decision,
    Escalate,
    Gate,
    Input,
    Node,
    Operation,
    Pipeline,
    Profile,
    Role,
    RunAgentSpec,
    RunNode,
    RunNodeKind,
    RunStage,
    Stage,
    TrackerAction,
)

GATE_KINDS = ("command", "tests", "coverage", "diff-limits", "document")
DECIDER_KINDS = tuple(deciders.KINDS)
REF_INPUTS = ("stage", "gate", "decision")

# The RunAgents a Pipeline file still has a block for, and the Describer is a Stage of the Run graph (ADR 0014).
PIPELINE_RUN_AGENTS = {CI_FIXER.name: CI_FIXER}

PIPELINE_FIELDS = (
    "start",
    "escalate_to",
    "defaults",
    *PIPELINE_RUN_AGENTS,
    "stages",
    "gates",
    "decisions",
    "operations",
)
# The fields of AgentSettings that every block of a Fresh agent sets, where `escalate` is a block of a Stage only.
AGENT_FIELDS = tuple(field.name for field in dataclasses.fields(AgentSettings) if field.name != "escalate")
SETTINGS_FIELDS = (*AGENT_FIELDS, "workspace")
DEFAULTS_FIELDS = (*SETTINGS_FIELDS, "sandbox_retries")
RUN_GRAPH_FIELDS = ("start", "escalate_to", "nodes")
RUN_NODE_FIELDS = ("kind", "with", "on_fail", "next")
RUN_STAGE_FIELDS = ("role", *AGENT_FIELDS, "next")
ROLE_FIELDS = ("prompt", "schema", "skills")
STAGE_FIELDS = ("role", *SETTINGS_FIELDS, "inputs", "next", "rounds", "escalate")
GATE_FIELDS = ("kind", "with", "clean", "reuse", "next", "from")
DECISION_FIELDS = ("decider", "outcomes", "inputs", "next")
DECIDER_FIELDS = ("kind", "role", *AGENT_FIELDS, "function", "with")
OPERATION_FIELDS = ("kind", "with", "on_fail", "next")
FILE_ISSUES_FIELDS = ("from", "labels", "milestone")
TRACKER_WITH_FIELDS = {ASSIGN: (), LABEL: ("add", "remove"), COMMENT: ("text",)}
INPUT_FIELDS = (*REF_INPUTS, "optional", "rounds")
ROUNDS_FIELDS = ("max", "then")
ESCALATE_FIELDS = tuple(field.name for field in dataclasses.fields(Escalate))
PROFILES = get_args(Profile)
RUNNABLE_GATE_KINDS = tuple(gates.KINDS)
RUNNABLE_DECIDER_KINDS = tuple(name for name, kind in deciders.KINDS.items() if kind.runnable)
RUNNABLE_OPERATION_KINDS = ("catch_up", "merge", "file_issues", *TRACKER_OPERATIONS)

# A Pipeline that names a retired Operation kind fails with the name the kind goes by now.
RETIRED_OPERATION_KINDS = {"sync": "catch_up"}

# A Run graph that names the retired Run node kind fails with what took its place.
RETIRED_RUN_NODE_KIND = "land"

VAR = re.compile(r"\$\{vars\.(\w+)\}")
REFERENCE = re.compile(r"\$\{([^}]*)\}")


class ConfigError(Exception):
    def __init__(self, *errors: str) -> None:
        super().__init__("\n".join(errors))
        self.errors = list(errors)


class Checks(Protocol):
    """Checks are what an Executor tells about the settings of its agents, which the Executor itself implements.

    `default_model` is the model it runs when the Pipeline names none, and `settings_problem` returns why it cannot run
    a model at a reasoning effort, or None when it can, since the efforts an Executor takes may differ per model.
    `reported_windows` names the subscription windows it may report, which a brake of the `throttle` may name.
    """

    @property
    def default_model(self) -> str: ...

    @property
    def reported_windows(self) -> tuple[str, ...]: ...

    def settings_problem(self, model: str, effort: str | None) -> str | None: ...


class Errors:
    """Errors collects messages that name the file and the place in it."""

    def __init__(self, path: Path, shared: list[str] | None = None) -> None:
        self.path = path
        self.messages = [] if shared is None else shared

    def at(self, path: Path) -> "Errors":
        """Returns a collector for another file that adds to the same list."""
        return Errors(path, self.messages)

    def add(self, where: str, message: str) -> None:
        shown = display_path(self.path)
        self.messages.append(f"{shown}: {where}: {message}" if where else f"{shown}: {message}")

    def pending(self, where: str, what: str) -> None:
        self.add(where, f"{what} is not supported yet")

    def fields(self, where: str, body: dict[str, Any], allowed: tuple[str, ...]) -> None:
        for key in body:
            if key not in allowed:
                self.add(join(where, key), "unknown field")

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


def apply_overrides(pipeline: dict[str, Any], overrides: dict[str, Any], errors: Errors) -> None:
    """Merges the project's overrides into the nodes of a Pipeline or Run graph, and only into their Stage, Gate,
    Decider and Run node fields."""
    allowed = {
        "stages": ("Stage", STAGE_FIELDS),
        "gates": ("Gate", GATE_FIELDS),
        "decisions": ("Decision", ()),
        "nodes": ("Run node", (*RUN_NODE_FIELDS, *RUN_STAGE_FIELDS)),
    }
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


def _overridable(where: str, patch: dict[str, Any], fields: tuple[str, ...], errors: Errors) -> dict[str, Any]:
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


def substitute(value: Any, variables: dict[str, Any], where: str, errors: Errors) -> Any:
    """Replaces `${vars.*}`; a string that is a single reference takes the variable's value with its type."""
    if isinstance(value, dict):
        return {key: substitute(item, variables, join(where, str(key)), errors) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, variables, f"{where}[{index}]", errors) for index, item in enumerate(value)]
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


class PipelineLoader:
    def __init__(self, errors: Errors, library: Library, checks: Mapping[str, Checks], executor: str) -> None:
        """Loads a Pipeline whose agents run on `executor` unless a block names another of `checks`, the Checks of
        each Executor by its name, whose models and efforts that Executor's Checks take."""
        self.errors = errors
        self.library = library
        self.checks = checks
        self.executor = executor
        self.roles: dict[str, Role] = {}

    def load(self, data: dict[str, Any]) -> Pipeline:
        errors = self.errors
        if "roles" in data:
            errors.add(
                "roles",
                "a Pipeline has no `roles:` section; move each Role to `roles/<name>.yaml` in a Layer of the Library,"
                " with `prompt` and `schema` naming files of `prompts/` and `schemas/` there",
            )
        if DESCRIBER.name in data:
            errors.add(
                DESCRIBER.name,
                "is now the node `describe` of the Run graph; set its fields under `overrides.nodes.describe` of the"
                " project file",
            )
        known = {k: v for k, v in data.items() if k not in ("roles", DESCRIBER.name)}
        errors.fields("", known, PIPELINE_FIELDS)
        self.defaults = errors.mapping("defaults", data.get("defaults", {}))
        errors.fields("defaults", self.defaults, DEFAULTS_FIELDS)
        # What a Stage runs with when neither `defaults` nor the Stage sets it, with the model of its Executor.
        builtin = self._builtin(self.executor)
        self.commits = self._commits("defaults", self.defaults, True)
        self.fallback = self._settings("defaults", self.defaults, builtin)

        nodes: dict[str, Node] = {}
        parsers: dict[str, Callable[[str, str, dict[str, Any]], Node]] = {
            "stages": self._stage,
            "gates": self._gate,
            "decisions": self._decision,
            "operations": self._operation,
        }
        for section, parse in parsers.items():
            self._nodes(data, section, parse, nodes, TERMINAL_NODES)
        start, escalate_to = self._start_and_escalation(data, nodes, TERMINAL_NODES)
        pipeline_checks.check(nodes, start, escalate_to, errors.add)
        run_agents = {
            name: self._run_agent(agent, data[name], nodes)
            for name, agent in PIPELINE_RUN_AGENTS.items()
            if name in data
        }
        pipeline_checks.check_roles(nodes, list(run_agents.values()), errors.add)
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
            run_agents=run_agents,
        )

    def load_run_graph(self, data: dict[str, Any], fallback: AgentSettings) -> Pipeline:
        """Reads a Run graph, whose format and check are a Pipeline's, and whose `nodes` take the kinds of a Run.

        A Run Stage runs with `fallback`, the `defaults` of the Ticket Pipeline, in what it leaves out.
        """
        errors = self.errors
        self.fallback = fallback
        errors.fields("", data, RUN_GRAPH_FIELDS)
        nodes: dict[str, Node] = {}
        self._nodes(data, "nodes", self._run_node, nodes, RUN_TERMINAL_NODES)
        start, escalate_to = self._start_and_escalation(data, nodes, RUN_TERMINAL_NODES)
        pipeline_checks.check(nodes, start, escalate_to, errors.add, RUN_TERMINAL_NODES)
        pipeline_checks.check_run_graph(nodes, start, escalate_to, errors.add)
        return Pipeline(path=errors.path, start=start, nodes=nodes, escalate_to=escalate_to)

    def _nodes(
        self,
        data: dict[str, Any],
        section: str,
        parse: Callable[[str, str, dict[str, Any]], Node],
        nodes: dict[str, Node],
        terminals: tuple[str, ...],
    ) -> None:
        """Parses the nodes of `section` into `nodes`, where a name is taken once and by no node of `terminals`."""
        for name, body in self._section(data, section).items():
            where = f"{section}.{name}"
            if name in nodes or name in terminals:
                self.errors.add(where, f"node name {name} is already taken")
                continue
            if (problem := pipeline_checks.node_name_problem(name)) is not None:
                self.errors.add(where, problem)
            nodes[name] = parse(where, name, self.errors.mapping(where, body))

    def _start_and_escalation(
        self, data: dict[str, Any], nodes: dict[str, Node], terminals: tuple[str, ...]
    ) -> tuple[str, str | None]:
        """Reads `start` and `escalate_to`, which must name a node, and `escalate_to` may name one of `terminals`."""
        start = str(data.get("start", ""))
        if not start:
            self.errors.add("", "missing start")
        elif start not in nodes:
            self.errors.add("start", f"names undeclared node {start}")
        escalate_to = data.get("escalate_to")
        if escalate_to is not None and escalate_to not in nodes and escalate_to not in terminals:
            self.errors.add("escalate_to", f"names undeclared node {escalate_to}")
        return start, escalate_to

    def _run_node(self, where: str, name: str, body: dict[str, Any]) -> RunNode | RunStage:
        """Reads a node of a Run graph: a Run Stage when it names a Role and no kind, else a node of its `kind`."""
        if "role" in body and "kind" not in body:
            return self._run_stage(where, name, body)
        self.errors.fields(where, body, RUN_NODE_FIELDS)
        if body.get("kind") == RETIRED_RUN_NODE_KIND:
            self.errors.add(where, f"kind {RETIRED_RUN_NODE_KIND} is now a Stage `describe` and a node of kind open_pr")
            return RunNode(name=name, kind=OPEN_PR, next={"opened": "ended", "failed": "ended"})
        kind = self._kind(where, body, tuple(RUN_NODE_KINDS), tuple(RUN_NODE_KINDS))
        tracker = self._tracker_action(where, body, kind, at_run=True)
        if tracker is not None:
            return RunNode(name=name, kind=kind, next=self._next(where, body)[0], tracker=tracker)
        self._no_on_fail(where, body)
        traits = RUN_NODE_KINDS.get(kind, RunNodeKind())
        with_ = self._block(f"{where}.with", body.get("with", {}), (*traits.with_fields, *traits.optional_with))
        for field in traits.with_fields:
            if field not in with_:
                self.errors.add(f"{where}.with", f"missing {field}")
        if kind == FILTER:
            with_ = self._ticket_filter(f"{where}.with", with_)
        routes, resets = self._next(where, body)
        return RunNode(name=name, kind=kind, next=routes, with_=with_, resets=resets)

    def _ticket_filter(self, at: str, with_: dict[str, Any]) -> dict[str, Any]:
        """Reads the `with` of a `filter`, whose `label` and `without` each name one label or a list of them."""
        lists = {key: [value] if isinstance(value, str) else value for key, value in with_.items()}
        found = {key: list(self._names(at, lists, key)) for key in ("label", "without")}
        if not found["label"] and not found["without"]:
            self.errors.add(at, "names no label to take or leave out")
        return found

    def _run_stage(self, where: str, name: str, body: dict[str, Any]) -> RunStage:
        """Reads a Run Stage, which has the settings of a Stage, reads no `inputs` and commits nothing."""
        self.errors.fields(where, body, RUN_STAGE_FIELDS)
        routes, resets = self._next(where, body)
        return RunStage(
            name=name,
            role=self._role_ref(where, body["role"]),
            settings=self._settings(where, body, self.fallback),
            inputs=(),
            next=routes,
            resets=resets,
            commits=False,
        )

    def _run_agent(self, agent: RunAgent, value: Any, nodes: dict[str, Node]) -> RunAgentSpec:
        """Reads the block of a RunAgent, which names a Role and sets the same fields of a Stage as `defaults` does.

        The CI fixer's block also sets the Rounds it may take and the Gates each Round must pass.
        """
        where = agent.name
        body = self._block(where, value, ("role", *AGENT_FIELDS, *agent.fields))
        if "role" not in body:
            self.errors.add(where, "missing role")
        role = self._role_ref(where, body.get("role"))
        settings = self._settings(where, body, self.fallback)
        rounds = agent.rounds
        if "rounds" in agent.fields and "rounds" in body:
            rounds = self._parsed(f"{where}.rounds", body["rounds"], values.positive) or rounds
        names = self._gate_names(where, body.get("gates", []), nodes) if "gates" in agent.fields else []
        return RunAgentSpec(run_agent=agent, role=role, settings=settings, rounds=rounds, gates=tuple(names))

    def _gate_names(self, where: str, named: Any, nodes: dict[str, Node]) -> list[str]:
        """Reads `gates`, the Gates of the Pipeline in the order they run, each after the Gate whose Probe it reads."""
        names: list[str] = []
        if not isinstance(named, list) or not all(isinstance(name, str) for name in named):
            self.errors.add(f"{where}.gates", "must be a list of Gate names")
        else:
            names = list(named)
        for index, name in enumerate(names):
            node = nodes.get(name)
            here = f"{where}.gates[{index}]"
            if not isinstance(node, Gate):
                self.errors.add(here, f"names undeclared Gate {name}")
            elif node.from_ is not None and node.from_ not in names[:index]:
                self.errors.add(here, f"Gate {name} reads the Probe of {node.from_}, which must come before it")
        return names

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
            body=found.body,
            schema=schema.document,
        )
        self.roles[name] = role
        return role

    def _stage(self, where: str, name: str, body: dict[str, Any]) -> Stage:
        self.errors.fields(where, body, STAGE_FIELDS)
        if "role" not in body:
            self.errors.add(where, "missing role")
        role = self._role_ref(where, body.get("role"))
        commits = self._commits(where, body, self.commits)
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
            effort = self._effort(f"{where}.escalate", block, None)
            escalate = Escalate(from_round=from_round or 1, model=str(model), effort=effort)
            if isinstance(model, str):
                escalated = dataclasses.replace(settings, escalate=escalate).for_round(escalate.from_round)
                self._check_pair(f"{where}.escalate", block, escalated, settings)
        return Stage(
            name=name,
            role=role,
            settings=dataclasses.replace(settings, escalate=escalate),
            inputs=inputs,
            next=routes,
            resets=resets,
            commits=commits,
            max_rounds=max_rounds,
        )

    def _block(self, where: str, value: Any, allowed: tuple[str, ...]) -> dict[str, Any]:
        block = self.errors.mapping(where, value)
        self.errors.fields(where, block, allowed)
        return block

    def _required[T](self, where: str, block: dict[str, Any], key: str, parse: Callable[[Any], T]) -> T | None:
        if key not in block:
            self.errors.add(where, f"missing {key}")
            return None
        return self._parsed(join(where, key), block[key], parse)

    def _parsed[T](self, where: str, value: Any, parse: Callable[[Any], T]) -> T | None:
        try:
            return parse(value)
        except values.Invalid as error:
            self.errors.add(where, str(error))
            return None

    def _commits(self, where: str, body: dict[str, Any], fallback: bool) -> bool:
        """Reads `workspace`, which tells whether a Stage's changes get a Stage boundary commit, or `fallback`."""
        if "workspace" not in body:
            return fallback
        if body["workspace"] not in ("rw", "ro"):
            self.errors.add(join(where, "workspace"), "must be rw or ro")
            return fallback
        return bool(body["workspace"] == "rw")

    def _builtin(self, executor: str) -> AgentSettings:
        """Returns the settings an agent on `executor` runs with when no block sets them, with the Executor's model."""
        return AgentSettings(model=self.checks[executor].default_model, executor=executor)

    def _settings(self, where: str, body: dict[str, Any], fallback: AgentSettings) -> AgentSettings:
        """Reads the fields of AgentSettings that a block sets, and takes the ones it leaves out from `fallback`.

        A block that names an Executor other than the fallback's takes its model and effort from that Executor's
        own defaults, since the fallback's pair is one of another Executor.
        """
        if "executor" in body:
            executor = self._parsed(join(where, "executor"), body["executor"], values.choice(tuple(self.checks)))
            if executor is not None and executor != fallback.executor:
                fallback = dataclasses.replace(
                    fallback, model=self._builtin(executor).model, effort=None, executor=executor
                )
        attempts = self._attempts(where, body, fallback.attempts)
        timeout = fallback.timeout
        if "timeout" in body:
            timeout = self._parsed(join(where, "timeout"), body["timeout"], values.duration) or fallback.timeout
        stall = fallback.stall
        if "stall" in body:
            stall = self._parsed(join(where, "stall"), body["stall"], values.duration) or fallback.stall
        # A Stage that declares neither value inherits a pair that `defaults` already checked.
        declared = where == "defaults" or "stall" in body or "timeout" in body
        if stall is not None and stall >= timeout and declared:
            self.errors.add(join(where, "stall"), f"must be less than timeout {timeout:g}s")
        profile = fallback.profile
        if "profile" in body:
            if body["profile"] not in PROFILES:
                self.errors.add(join(where, "profile"), "must be project or hermetic")
            else:
                profile = body["profile"]
        settings = AgentSettings(
            model=str(body.get("model", fallback.model)),
            executor=fallback.executor,
            effort=self._effort(where, body, fallback.effort),
            profile=profile,
            attempts=attempts,
            timeout=timeout,
            stall=stall,
        )
        self._check_pair(where, body, settings, fallback)
        return settings

    def _check_pair(self, where: str, body: dict[str, Any], settings: AgentSettings, fallback: AgentSettings) -> None:
        """Reports a model and effort pair of `settings` that their Executor cannot run.

        The pair is the one after the merge with `fallback`, so a model that `body` sets is checked with the effort it
        inherits and the other way round.
        A problem the inherited pair already has on the same Executor was reported where that pair is set.
        A problem lands on `effort` when the model alone runs and the block sets the effort, else on `model`.
        """
        checks = self.checks[settings.executor]
        problem = checks.settings_problem(settings.model, settings.effort)
        same_executor = settings.executor == fallback.executor
        inherited = checks.settings_problem(fallback.model, fallback.effort) if same_executor else None
        if problem is None or problem == inherited:
            return
        alone = checks.settings_problem(settings.model, None) is None
        self.errors.add(join(where, "effort" if alone and "effort" in body else "model"), problem)

    def _effort(self, where: str, body: dict[str, Any], fallback: str | None) -> str | None:
        """Reads the `effort` of a block, or `fallback` when it sets none."""
        if "effort" not in body:
            return fallback
        effort = body["effort"]
        if not isinstance(effort, str):
            self.errors.add(join(where, "effort"), "must be a string")
            return None
        return effort

    def _attempts(self, where: str, body: dict[str, Any], fallback: int) -> int:
        if "attempts" not in body:
            return fallback
        return self._parsed(join(where, "attempts"), body["attempts"], values.positive) or fallback

    def _gate(self, where: str, name: str, body: dict[str, Any]) -> Gate:
        self.errors.fields(where, body, GATE_FIELDS)
        kind = self._kind(where, body, GATE_KINDS, RUNNABLE_GATE_KINDS)
        with_ = self.errors.mapping(f"{where}.with", body.get("with", {}))
        if kind in gates.KINDS:
            with_, errors = gates.resolve_with(kind, with_)
            for field, message in errors:
                self.errors.add(join(f"{where}.with", field), message)
        clean = body.get("clean", False)
        if not isinstance(clean, bool):
            self.errors.add(f"{where}.clean", "must be true or false")
        reuse = body.get("reuse", True)
        if not isinstance(reuse, bool):
            self.errors.add(f"{where}.reuse", "must be true or false")
        from_ = self._from(where, body, kind, with_)
        routes, resets = self._next(where, body)
        return Gate(
            name=name,
            kind=kind,
            with_=with_,
            clean=clean is True,
            reuse=reuse is not False,
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
            decider=Decider(kind=kind, role=role, settings=settings, function=function, with_=with_),
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
            self.errors.add(join(f"{at}.with", field), message)
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
        if (renamed := RETIRED_OPERATION_KINDS.get(str(body.get("kind")))) is not None:
            self.errors.add(where, f"kind {body['kind']} is now {renamed}")
            body = {**body, "kind": renamed}
        kind = self._kind(where, body, tuple(OPERATION_OUTCOMES), RUNNABLE_OPERATION_KINDS)
        routes, resets = self._next(where, body)

        # The Orchestrator advances only active Tickets, so a node after `merged` would never run.
        merged = routes.get("merged")
        if kind == "merge" and merged not in (None, "done"):
            self.errors.add(where, f"outcome merged routes to {merged}, and a merged Ticket can only go to done")
        tracker = self._tracker_action(where, body, kind, at_run=False)
        if tracker is not None:
            return Operation(name=name, kind=kind, next=routes, resets=resets, tracker=tracker)
        self._no_on_fail(where, body)
        from_, labels, milestone = self._file_issues(where, body) if kind == "file_issues" else ((), (), None)
        if kind != "file_issues" and "with" in body:
            self.errors.add(f"{where}.with", f"an Operation of kind {kind} has no with")
        return Operation(
            name=name, kind=kind, next=routes, resets=resets, from_=from_, labels=labels, milestone=milestone
        )

    def _no_on_fail(self, where: str, body: dict[str, Any]) -> None:
        if "on_fail" in body:
            self.errors.add(f"{where}.on_fail", "only a Tracker Operation has on_fail")

    def _tracker_action(self, where: str, body: dict[str, Any], kind: str, at_run: bool) -> TrackerAction | None:
        """Reads what a Tracker Operation does, or returns None for a node of another kind.

        Only a Run node has a `target`, because in a Pipeline the Operation acts on its own Ticket.
        """
        if kind not in TRACKER_OPERATIONS:
            return None
        at = f"{where}.with"
        with_ = self._block(at, body.get("with", {}), (*TRACKER_WITH_FIELDS[kind], *(("target",) if at_run else ())))
        on_spec = "target" in with_
        if on_spec and with_["target"] != SPEC_TARGET:
            self.errors.add(f"{at}.target", f"must be {SPEC_TARGET}")
        on_fail = self._parsed(f"{where}.on_fail", body.get("on_fail", WARN), values.choice(ON_FAIL)) or WARN
        add, remove = self._names(at, with_, "add"), self._names(at, with_, "remove")
        if kind == LABEL and not add and not remove:
            self.errors.add(at, "names no label to add or remove")
        text = self._comment_text(at, with_, on_spec) if kind == COMMENT else ""
        return TrackerAction(kind, add, remove, text, on_spec, on_fail)

    def _names(self, at: str, with_: dict[str, Any], key: str) -> tuple[str, ...]:
        value = with_.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            self.errors.add(f"{at}.{key}", "must be a list of names")
            return ()
        return tuple(value)

    def _comment_text(self, at: str, with_: dict[str, Any], on_spec: bool) -> str:
        """Reads the static text of a `comment`, which may name only the references a comment on its target has."""
        text = with_.get("text")
        if not isinstance(text, str) or not text:
            self.errors.add(f"{at}.text", "must be a text")
            return ""
        for name in REFERENCE.findall(text):
            if name not in COMMENT_REFERENCES:
                self.errors.add(f"{at}.text", f"unknown reference ${{{name}}}")
            elif name == "ticket" and on_spec:
                self.errors.add(f"{at}.text", "a comment on the Spec has no reference ${ticket}")
        return text

    def _file_issues(self, where: str, body: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...], str | None]:
        """Reads the `with` of `file_issues`: the Stages it reads, the labels of every issue and their milestone."""
        at = f"{where}.with"
        with_ = self.errors.mapping(at, body.get("with", {}))
        self.errors.fields(at, with_, FILE_ISSUES_FIELDS)
        lists: dict[str, tuple[str, ...]] = {}
        for key in ("from", "labels"):
            value = with_.get(key, [])
            if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
                self.errors.add(f"{at}.{key}", "must be a list of names")
                value = []
            lists[key] = tuple(value)
        if not lists["from"]:
            self.errors.add(f"{at}.from", "must name the Stages whose issues it files")
        milestone = with_.get("milestone")
        if milestone is not None and (not isinstance(milestone, str) or not milestone):
            self.errors.add(f"{at}.milestone", "must be the title of a milestone")
            milestone = None
        return lists["from"], lists["labels"], milestone

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


def with_describer(pipeline: Pipeline, run_graph: Pipeline, errors: Errors) -> Pipeline:
    """Returns the Ticket Pipeline with the Run graph's Stage as its Describer, whose Role the check holds against it.

    So the Executors and the Roles of a Run are those of one Pipeline, whichever level names the agent.
    """
    stage = run_graph.run_stage
    if stage is None:
        return pipeline
    spec = RunAgentSpec(run_agent=DESCRIBER, role=stage.role, settings=stage.settings)
    pipeline_checks.check_run_agent(f"nodes.{stage.name}", spec, pipeline.nodes, errors.at(run_graph.path).add)
    return dataclasses.replace(pipeline, run_agents={DESCRIBER.name: spec, **pipeline.run_agents})


def display_path(path: Path) -> str:
    """Shows a path relative to the working directory when that is shorter."""
    relative = os.path.relpath(path)
    return relative if len(relative) < len(str(path)) else str(path)


def join(where: str, key: str) -> str:
    return ".".join(part for part in (where, key) if part)


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as error:
        raise ConfigError(f"cannot read {display_path(path)}: {error.strerror}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"{display_path(path)} is not valid YAML: {error}") from error
    if not isinstance(data, dict):
        raise ConfigError(f"{display_path(path)} must hold a YAML mapping")
    return data
