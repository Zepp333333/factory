import json
from typing import Any

import pytest
import yaml

from factory.config import display_path
from factory.ports.kind import Kind
from harness import AgentScript, Harness, ScriptedExecutor
from test_describer import set_describe

HINT = "use sonnet, opus, haiku, opusplan or a claude- model ID"
TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"


def errors_of(factory: Harness) -> list[str]:
    """Returns the error lines of `factory check` without the program prefix and the pipeline path."""
    result = factory.check()
    assert result.code != 0, result.out
    prefix = f"factory: {display_path(factory.pipeline_file)}: "
    run_graph = f"factory: {display_path(factory.factory_library / 'runs' / 'default.yaml')}: "
    return [line.removeprefix(prefix).removeprefix(run_graph) for line in result.err.splitlines()]


def review_stage(**input_fields: object) -> dict[str, object]:
    return {"role": "implementer", "inputs": [{"stage": "implement", **input_fields}], "next": {"done": "merge"}}


def test_check_accepts_a_valid_config_whose_pipeline_is_given_by_a_relative_or_an_absolute_path(
    factory: Harness,
) -> None:
    relative = factory.check()
    factory.project["pipeline"] = {"file": str(factory.pipeline_file)}
    absolute = factory.check()

    for result in (relative, absolute):
        assert result.code == 0, result.err
        assert result.out.splitlines() == [f"{display_path(factory.project_file)}: OK"]


def test_outcome_without_a_route_is_an_error(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"] = {}
    factory.pipeline["operations"]["merge"]["next"] = {}

    assert errors_of(factory) == [
        "stages.implement: outcome done has no route",
        "operations.merge: outcome merged has no route",
    ]


def test_merged_must_route_to_done(factory: Harness) -> None:
    factory.pipeline["operations"]["merge"]["next"]["merged"] = "smoke"
    smoke = {"kind": "command", "with": {"run": "true"}, "next": {"pass": "done", "fail": "done"}}
    factory.pipeline["gates"] = {"smoke": smoke}

    assert errors_of(factory) == [
        "operations.merge: outcome merged routes to smoke, and a merged Ticket can only go to done",
    ]


def test_escalate_to_routes_only_escalating_outcomes(factory: Harness) -> None:
    del factory.pipeline["escalate_to"]
    factory.pipeline["stages"]["implement"]["rounds"] = {"max": 2}

    assert errors_of(factory) == [
        "stages.implement: outcome exhausted has no route",
        "stages.implement: outcome failed has no route",
    ]


def test_route_to_an_undeclared_node_or_from_an_unknown_outcome_is_an_error(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"] = {"done": "build", "passed": "merge"}

    assert errors_of(factory) == [
        "stages.implement: outcome done routes to undeclared node build",
        "stages.implement: there is no outcome passed",
    ]


def test_required_input_missing_on_some_path_is_an_error(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"] = {"done": "review", "failed": "review"}
    factory.pipeline["stages"]["review"] = review_stage()

    assert "stages.review: required input stage implement can be missing on some path" in errors_of(factory)


def test_required_input_on_every_path_and_optional_input_are_not_path_errors(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"] = {"done": "review", "failed": "check"}
    factory.pipeline["stages"]["review"] = review_stage()
    factory.pipeline["stages"]["check"] = review_stage(optional=True)

    result = factory.check()

    assert result.code == 0, result.err


def test_cause_has_no_source_at_the_start_node(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["inputs"] = ["spec", "ticket", "cause"]

    assert "stages.implement: input cause has no source at the start node" in errors_of(factory)


def test_input_naming_an_undeclared_node_is_an_error(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["inputs"] = ["spec", "plan", {"decision": "triage"}]

    errors = errors_of(factory)

    assert "stages.implement: unknown input plan" in errors
    assert "stages.implement: input decision triage is not declared" in errors


def test_unknown_variable_is_an_error(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["model"] = "${vars.model}"
    factory.pipeline["operations"]["merge"]["kind"] = "${vars.merge}"
    factory.project["vars"] = {"other": "x"}

    assert errors_of(factory) == [
        "operations.merge.kind: unknown variable merge",
        "stages.implement.model: unknown variable model",
    ]


def test_run_takes_vars_from_the_project_file_and_overrides_that_merge_dicts_and_replace_lists(
    factory: Harness,
) -> None:
    """The Pipeline sets the model through a var, and an override sets the inputs through a var.

    The override merges into `next`, so `done` keeps its route, and replaces the list of inputs.
    Without `escalate_to`, the check passes only because the override routes `failed`.
    """
    del factory.pipeline["escalate_to"]
    factory.pipeline["defaults"]["model"] = "${vars.model}"
    factory.pipeline["stages"]["implement"]["model"] = "claude-${vars.size}"
    factory.project["vars"] = {"model": "haiku", "size": "opus", "input": "ticket"}
    factory.project["overrides"] = {
        "stages": {"implement": {"inputs": ["${vars.input}"], "next": {"failed": "abandoned"}}}
    }
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})

    assert factory.check().code == 0
    assert factory.run(spec).code == 0
    [request] = factory.executor.requests
    assert request.settings.model == "claude-opus"
    assert [i["source"] for i in json.loads(request.brief.read_text())["inputs"]] == ["ticket"]


def test_overrides_cannot_change_roles_or_unknown_nodes(factory: Harness) -> None:
    factory.project["overrides"] = {
        "roles": {"implementer": {"prompt": "other.md"}},
        "stages": {"implement": {"prompt": "other.md", "schema": "other"}, "review": {"model": "opus"}},
        "decisions": {"triage": {"decider": {"model": "opus"}}},
    }

    result = factory.check()

    assert result.code != 0
    prefix = f"factory: {display_path(factory.project_file)}: "
    assert [line.removeprefix(prefix) for line in result.err.splitlines()] == [
        "overrides.decisions.triage: there is no such Decision",
        "overrides.roles: roles cannot be overridden, only stages, gates and decisions",
        "overrides.stages.implement.prompt: a Role field cannot be overridden",
        "overrides.stages.implement.schema: a Role field cannot be overridden",
        "overrides.stages.review: there is no such Stage",
    ]


def test_consilium_is_refused_as_not_implemented_in_v1(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = "vote"
    factory.pipeline["decisions"] = {
        "vote": {
            "decider": {"kind": "consilium", "role": "implementer"},
            "outcomes": ["accept"],
            "next": {"accept": "merge"},
        }
    }

    assert errors_of(factory) == ["decisions.vote: Decider kind consilium is not implemented in v1"]


def test_unknown_role_is_an_error(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["role"] = "tester"

    assert errors_of(factory) == ["stages.implement: role tester is in no Layer"]


def test_role_with_a_missing_prompt_or_a_broken_schema_is_an_error(factory: Harness) -> None:
    factory.pipeline["roles"]["acceptor"] = {"prompt": "prompts/implementer.md", "output": "schemas/broken.json"}
    (factory.config / "schemas" / "broken.json").write_text('{"type": 7}')
    factory.pipeline["roles"]["ruler"] = {"prompt": "prompts/implementer.md", "output": "schemas/missing.json"}
    factory.pipeline["stages"]["implement"]["role"] = "acceptor"
    factory.pipeline["stages"]["again"] = {**factory.pipeline["stages"]["implement"], "role": "ruler"}
    factory.pipeline["stages"]["again"]["next"] = {"done": "merge"}
    factory.pipeline["stages"]["implement"]["next"] = {"done": "again"}

    assert errors_of(factory) == [
        "stages.again: role ruler in factory names schema ruler, which is in no Layer",
        "stages.implement: the schema of Role acceptor is not a valid JSON Schema:"
        " 7 is not valid under any of the given schemas",
    ]


def test_a_roles_section_in_a_pipeline_says_how_to_move_it(factory: Harness) -> None:
    factory.keep_roles = True

    result = factory.check()

    assert result.code == 2
    assert result.err.splitlines() == [
        f"factory: {display_path(factory.pipeline_file)}: roles: a Pipeline has no `roles:` section;"
        " move each Role to `roles/<name>.yaml` in a Layer of the Library,"
        " with `prompt` and `schema` naming files of `prompts/` and `schemas/` there"
    ]


def test_check_reports_features_this_engine_does_not_run_yet(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {"build": {"kind": "diff-limits", "with": {}, "next": {"pass": "merge"}}}

    assert errors_of(factory) == [
        "gates.build: kind diff-limits is not supported yet",
        "gates.build: outcome fail has no route",
    ]


def test_profile_is_project_or_hermetic(factory: Harness) -> None:
    factory.pipeline["defaults"]["profile"] = "hermetic"
    factory.pipeline["stages"]["implement"]["profile"] = "owner"

    assert errors_of(factory) == ["stages.implement.profile: must be project or hermetic"]


def test_sandbox_retries_is_a_non_negative_integer(factory: Harness) -> None:
    factory.pipeline["defaults"]["sandbox_retries"] = -1

    assert errors_of(factory) == ["defaults.sandbox_retries: must be a non-negative integer"]

    factory.pipeline["defaults"]["sandbox_retries"] = 0

    assert factory.check().code == 0


def test_sandbox_retries_is_set_only_in_defaults(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["sandbox_retries"] = 5

    assert errors_of(factory) == ["stages.implement.sandbox_retries: unknown field"]


def test_sandbox_env_is_checked(factory: Harness) -> None:
    factory.project["sandbox"] = {"kind": "sbx", "env": {"OK": "1", "FACTORY_SANDBOX": "x", "BAD NAME": "x", "N": 3}}
    project = display_path(factory.project_file)

    assert factory.check().err.splitlines() == [
        f"factory: {project}: sandbox.env: 'BAD NAME' is not a variable name",
        f"factory: {project}: sandbox.env.FACTORY_SANDBOX: names are reserved for the factory",
        f"factory: {project}: sandbox.env.N: must be a string",
    ]


def test_sandbox_env_that_clashes_with_the_executor_is_refused_by_check_and_when_showing_a_role(
    factory: Harness,
) -> None:
    factory.project["sandbox"] = {"kind": "sbx", "env": {"CLAUDE_CODE_OAUTH_TOKEN": "mine"}}

    for result in (factory.check(), factory.cli("check", "--project", str(factory.project_file), "--show", "reviewer")):
        assert result.code == 2
        assert result.err == "factory: the Executor and the project file both set CLAUDE_CODE_OAUTH_TOKEN\n"


def test_sandbox_block_is_checked(factory: Harness) -> None:
    factory.project["sandbox"] = {"kind": "sbx", "cpus": 0, "memory": "8 GB", "egress": "pypi.org", "image": "x"}
    project = display_path(factory.project_file)

    assert factory.check().err.splitlines() == [
        f"factory: {project}: sandbox.image: unknown field",
        f"factory: {project}: sandbox.cpus: must be a positive integer",
        f"factory: {project}: sandbox.memory: must be a size such as 512m or 8g",
        f"factory: {project}: sandbox.egress: must be a list of hosts",
    ]


def test_sbx_fields_do_not_apply_to_a_local_sandbox(factory: Harness) -> None:
    factory.project["sandbox"] = {"kind": "local", "egress": ["pypi.org"]}

    assert factory.check().err == (
        f"factory: {display_path(factory.project_file)}: sandbox.egress: applies only to kind sbx\n"
    )


@pytest.mark.parametrize("caches", [".venv", ["build/out"], [".."], [""], [1]])
def test_caches_must_be_directory_names(factory: Harness, caches: Any) -> None:
    factory.project["caches"] = caches

    assert factory.check().err == (
        f"factory: {display_path(factory.project_file)}: caches: "
        "must be a list of directory names such as .venv or __pycache__\n"
    )


def test_unknown_fields_are_errors(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["modle"] = "opus"
    factory.pipeline["stage"] = {}

    assert errors_of(factory) == ["stage: unknown field", "stages.implement.modle: unknown field"]


def test_run_with_an_invalid_config_creates_no_run_and_leaves_git_alone(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.pipeline["stages"]["implement"]["next"] = {}
    factory.pipeline["operations"]["merge"]["next"] = {}
    before = factory.branches()

    result = factory.run(spec)

    assert result.code == 2
    assert result.out == ""
    assert len(result.err.splitlines()) == 2
    assert factory.branches() == before
    assert not factory.home.exists()
    assert factory.git("status", "--porcelain") == ""


def test_cause_after_an_outcome_without_artifact_is_an_error(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"] = {"done": "merge", "failed": "rework"}
    factory.pipeline["stages"]["rework"] = {"role": "implementer", "inputs": ["cause"], "next": {"done": "merge"}}

    assert "stages.rework: input cause can be missing on some path" in errors_of(factory)


def test_start_escalate_to_and_node_names_must_be_declared_once(factory: Harness) -> None:
    factory.pipeline["start"] = "plan"
    factory.pipeline["escalate_to"] = "human"
    factory.pipeline["operations"]["implement"] = {"kind": "catch_up", "next": {"clean": "done", "conflict": "done"}}
    factory.pipeline["operations"]["done"] = {"kind": "merge", "next": {"merged": "done"}}

    assert errors_of(factory) == [
        "operations.done: node name done is already taken",
        "operations.implement: node name implement is already taken",
        "start: names undeclared node plan",
        "escalate_to: names undeclared node human",
    ]


def test_decision_declares_a_non_empty_list_of_unreserved_outcomes(factory: Harness) -> None:
    factory.pipeline["decisions"] = {
        "empty": {"decider": {"kind": "human"}, "outcomes": [], "next": {}},
        "loud": {"decider": {"kind": "human"}, "outcomes": ["accept", "failed"], "next": {"accept": "done"}},
    }

    errors = errors_of(factory)

    assert "decisions.empty.outcomes: must be a non-empty list" in errors
    assert "decisions.loud.outcomes: failed is reserved and must not be declared" in errors


def test_overrides_change_only_the_decider_of_a_decision(factory: Harness) -> None:
    factory.pipeline["decisions"] = {
        "triage": {"decider": {"kind": "human"}, "outcomes": ["accept"], "next": {"accept": "done"}}
    }
    factory.project["overrides"] = {"decisions": {"triage": {"next": {"accept": "abandoned"}}}}

    result = factory.check()

    prefix = f"factory: {display_path(factory.project_file)}: "
    assert prefix + "overrides.decisions.triage.next: only the Decider of a Decision can be overridden" in result.err


def test_unknown_variable_in_overrides_points_at_the_project_file(factory: Harness) -> None:
    factory.project["overrides"] = {"stages": {"implement": {"model": "${vars.model}"}}}

    result = factory.check()

    assert result.err.splitlines() == [
        f"factory: {display_path(factory.project_file)}: overrides.stages.implement.model: unknown variable model"
    ]


def test_errors_found_before_an_unreadable_pipeline_are_kept(factory: Harness) -> None:
    factory.project = {"pipeline": {"file": "missing.yaml"}, "sandbox": {"kind": "docker"}}
    project = display_path(factory.project_file)

    result = factory.check()

    assert result.err.splitlines() == [
        f"factory: {project}: missing base",
        f"factory: {project}: sandbox.kind: must be local or sbx",
        f"factory: cannot read {display_path(factory.repo / 'missing.yaml')}: No such file or directory",
    ]


def test_overrides_of_a_section_that_is_not_a_mapping_are_errors(factory: Harness) -> None:
    factory.pipeline["gates"] = []
    factory.project["overrides"] = {"gates": {"build": {"with": {"run": "make"}}}}

    result = factory.check()

    assert f"factory: {display_path(factory.project_file)}: overrides.gates.build: there is no such Gate" in result.err
    assert "gates: must be a mapping" in result.err


def gate(factory: Harness, **body: object) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {"build": {"kind": "command", "next": {"pass": "merge", "fail": "abandoned"}, **body}}


def test_gate_with_is_checked_against_its_kind_after_vars_are_substituted(factory: Harness) -> None:
    gate(factory, **{"with": {"run": "${vars.build}", "timeout": "${vars.timeout}", "retries": 3}})
    factory.project["vars"] = {"build": "make", "timeout": "half an hour"}

    assert errors_of(factory) == [
        "gates.build.with.retries: unknown field",
        "gates.build.with.timeout: must be a duration such as 90s, 45m or 2h",
    ]


def test_gate_with_must_name_what_its_kind_requires(factory: Harness) -> None:
    gate(factory, **{"with": {"timeout": "5m"}})

    assert errors_of(factory) == ["gates.build.with: missing run"]


def test_overrides_change_one_field_of_a_gate_with(factory: Harness) -> None:
    gate(factory, **{"with": {"run": "make", "timeout": "5m"}})
    factory.project["overrides"] = {"gates": {"build": {"with": {"timeout": "soon"}}}}

    assert errors_of(factory) == ["gates.build.with.timeout: must be a duration such as 90s, 45m or 2h"]


def test_gate_refuses_attempts_and_bad_clean(factory: Harness) -> None:
    gate(factory, **{"with": {"run": "make"}, "attempts": 2, "clean": "yes"})

    assert errors_of(factory) == [
        "gates.build.attempts: unknown field",
        "gates.build.clean: must be true or false",
    ]


def test_gate_needs_escalate_to_for_a_lost_probe(factory: Harness) -> None:
    del factory.pipeline["escalate_to"]
    factory.pipeline["stages"]["implement"]["next"]["failed"] = "abandoned"
    gate(factory, **{"with": {"run": "make"}})

    assert errors_of(factory) == ["gates.build: a lost Probe escalates, so the Pipeline needs escalate_to"]


def fix_loop(factory: Harness, **fix: object) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["stages"]["fix"] = {"role": "implementer", "inputs": ["cause"], "next": {"done": "build"}, **fix}
    factory.pipeline["gates"] = {
        "build": {"kind": "command", "with": {"run": "make"}, "next": {"pass": "merge", "fail": "fix"}}
    }


def test_exhausted_is_an_outcome_only_of_a_stage_with_rounds(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"]["exhausted"] = "abandoned"

    assert errors_of(factory) == ["stages.implement: there is no outcome exhausted"]


def test_rounds_and_escalate_are_checked(factory: Harness) -> None:
    fix_loop(factory, rounds={"max": 0, "then": "abandoned", "min": 1}, escalate={"from_round": "two"})

    assert errors_of(factory) == [
        "stages.fix.rounds.min: unknown field",
        "stages.fix.rounds.max: must be a positive integer",
        "stages.fix.escalate.from_round: must be a positive integer",
        "stages.fix.escalate: missing model",
    ]


def test_rounds_then_and_next_exhausted_cannot_both_route(factory: Harness) -> None:
    fix_loop(factory, rounds={"max": 3, "then": "abandoned"})
    factory.pipeline["stages"]["fix"]["next"]["exhausted"] = "merge"

    assert errors_of(factory) == ["stages.fix: outcome exhausted is routed by both rounds.then and next"]


def test_rounds_without_then_escalate_when_exhausted(factory: Harness) -> None:
    fix_loop(factory, rounds={"max": 3})

    assert factory.check().code == 0


def test_reset_rounds_names_stages_with_a_round_limit(factory: Harness) -> None:
    fix_loop(factory, rounds={"max": 3, "then": "abandoned"})
    factory.pipeline["gates"]["build"]["next"]["fail"] = "triage"
    factory.pipeline["decisions"] = {
        "triage": {
            "decider": {"kind": "human"},
            "outcomes": ["rework"],
            "next": {"rework": {"to": "fix", "reset_rounds": ["fix", "implement", "x"]}},
        }
    }

    assert errors_of(factory) == [
        "decisions.triage.next.rework: reset_rounds names implement, which has no rounds limit",
        "decisions.triage.next.rework: reset_rounds names x, which is not a Stage",
    ]


def test_reset_rounds_on_a_loop_with_no_decision_is_an_error(factory: Harness) -> None:
    fix_loop(factory, rounds={"max": 1, "then": "rework"})
    factory.pipeline["stages"]["rework"] = {
        "role": "implementer",
        "inputs": ["ticket"],
        "next": {"done": {"to": "build", "reset_rounds": ["fix"]}},
    }
    factory.pipeline["gates"]["build"]["next"]["fail"] = {"to": "fix", "reset_rounds": ["fix"]}

    assert errors_of(factory) == [
        "stages.rework.next.done: reset_rounds names fix, which leads back here with no Decision in between",
        "gates.build.next.fail: reset_rounds names fix, which leads back here with no Decision in between",
    ]


def test_route_written_as_a_mapping_needs_no_reset(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = {"to": "merge"}

    assert factory.check().code == 0


def test_escalate_must_be_reachable_and_name_a_model(factory: Harness) -> None:
    fix_loop(factory, rounds={"max": 2}, escalate={"from_round": 3, "model": 5})

    assert errors_of(factory) == [
        "stages.fix.escalate.model: must be a string",
        "stages.fix.escalate.from_round: is past rounds.max 2",
    ]


def test_stages_that_run_out_of_rounds_into_each_other_are_an_error(factory: Harness) -> None:
    fix_loop(factory, rounds={"max": 3, "then": "rework"})
    factory.pipeline["stages"]["rework"] = {
        "role": "implementer",
        "inputs": ["ticket"],
        "rounds": {"max": 1, "then": "fix"},
        "next": {"done": "build"},
    }

    assert errors_of(factory) == [
        "stages.fix: running out of rounds loops through fix, rework, fix",
        "stages.rework: running out of rounds loops through rework, fix, rework",
    ]


def test_a_route_on_exhausted_that_resets_rounds_ends_a_loop_of_stages_out_of_rounds(factory: Harness) -> None:
    fix_loop(
        factory, rounds={"max": 3}, next={"done": "build", "exhausted": {"to": "rework", "reset_rounds": ["last"]}}
    )
    factory.pipeline["stages"]["rework"] = {
        "role": "implementer",
        "inputs": ["ticket"],
        "rounds": {"max": 1, "then": "fix"},
        "next": {"done": "build"},
    }
    factory.pipeline["stages"]["last"] = {
        "role": "implementer",
        "inputs": ["ticket"],
        "rounds": {"max": 1},
        "next": {"done": "merge"},
    }

    result = factory.check()

    assert result.code == 0, result.err


def test_cause_after_exhausted_is_the_cause_that_entered_the_stage(factory: Harness) -> None:
    fix_loop(factory, rounds={"max": 3, "then": "ruling"})
    factory.pipeline["stages"]["ruling"] = {"role": "implementer", "inputs": ["cause"], "next": {"done": "merge"}}

    assert factory.check().code == 0

    factory.pipeline["stages"]["implement"]["rounds"] = {"max": 1, "then": "fix"}

    assert errors_of(factory) == [
        "stages.fix: input cause can be missing on some path",
        "stages.ruling: input cause can be missing on some path",
    ]


def test_parallel_tickets_need_a_catch_up_on_every_way_into_merge(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["operations"]["catch_up"]["next"]["clean"] = "build"
    factory.pipeline["gates"] = {
        "build": {"kind": "command", "with": {"run": "true"}, "next": {"pass": "merge", "fail": "abandoned"}}
    }

    result = factory.check()

    assert result.code == 2
    assert result.err.splitlines() == [
        f"factory: {display_path(factory.project_file)}: max_parallel: 2 Tickets at once need a catch_up before merge, "
        "but stages.implement reaches operations.merge without one"
    ]


def test_one_ticket_at_a_time_merges_without_catch_up(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = "merge"
    del factory.pipeline["operations"]["catch_up"]

    result = factory.check()

    assert result.code == 0, result.err


@pytest.mark.parametrize(
    ("target", "body"),
    [
        (
            "look",
            {"role": "implementer", "inputs": ["spec", "ticket"], "workspace": "ro", "next": {"done": "catch_up"}},
        ),
        ("smoke", {"kind": "command", "with": {"run": "true"}, "next": {"pass": "catch_up", "fail": "abandoned"}}),
    ],
)
def test_catch_up_conflict_must_go_to_a_stage_that_commits(
    factory: Harness, target: str, body: dict[str, object]
) -> None:
    section = "stages" if "role" in body else "gates"
    factory.pipeline.setdefault(section, {})[target] = body
    factory.pipeline["operations"]["catch_up"]["next"]["conflict"] = target

    assert errors_of(factory) == [
        f"operations.catch_up: outcome conflict goes to {target}, "
        "but only a rw Stage can finish the merge that catch_up leaves in the Workspace"
    ]


def test_a_bare_pipeline_path_in_the_project_file_says_to_use_file(factory: Harness) -> None:
    factory.project["pipeline"] = "../config/pipeline.yaml"

    result = factory.check()

    assert result.code == 2
    assert result.err.splitlines() == [
        f"factory: {display_path(factory.project_file)}: pipeline: is the path ../config/pipeline.yaml;"
        " write `pipeline: {file: ../config/pipeline.yaml}`,"
        " or put the file in `pipelines/` of a Layer of the Library and name it"
    ]


def test_pipeline_named_in_the_project_file_is_found_in_the_library_or_is_an_error(factory: Harness) -> None:
    factory.project["pipeline"] = "mine"

    result = factory.check()

    assert result.code == 2
    assert result.err.splitlines() == [
        f"factory: {display_path(factory.project_file)}: pipeline: Pipeline mine is in no Layer of the Library"
    ]

    pipelines = factory.repo / ".factory" / "pipelines"
    pipelines.mkdir(parents=True)
    (pipelines / "mine.yaml").write_text(yaml.safe_dump({k: v for k, v in factory.pipeline.items() if k != "roles"}))

    assert factory.check().code == 0


def test_overrides_assign_a_stage_another_role_only_when_the_library_has_it(factory: Harness) -> None:
    factory.pipeline["roles"]["other"] = factory.pipeline["roles"]["implementer"]
    factory.project["overrides"] = {"stages": {"implement": {"role": "other"}}}

    assert factory.check().code == 0

    factory.project["overrides"] = {"stages": {"implement": {"role": "reviewer-single"}}}

    assert errors_of(factory) == ["stages.implement: role reviewer-single is in no Layer"]


def put_role(
    factory: Harness,
    name: str,
    schema: dict[str, Any],
    reads: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> None:
    """Writes a Role with its own prompt and schema to the factory Layer."""
    library = factory.factory_library
    for kind, suffix, text in (("prompts", "md", "Work.\n"), ("schemas", "json", json.dumps(schema))):
        (library / kind).mkdir(parents=True, exist_ok=True)
        (library / kind / f"{name}.{suffix}").write_text(text)
    (library / "roles").mkdir(parents=True, exist_ok=True)
    data = {
        "prompt": name,
        "schema": name,
        "skills": [],
        **({"reads": reads} if reads else {}),
        **({"body": body} if body else {}),
    }
    (library / "roles" / f"{name}.yaml").write_text(yaml.safe_dump(data))


def object_schema(required: list[str], **properties: Any) -> dict[str, Any]:
    return {"type": "object", "required": required, "properties": properties, "additionalProperties": False}


def reading(factory: Harness, reads: dict[str, Any], inputs: list[Any], **fields: Any) -> None:
    """Adds a Stage `review` with a Role that has `reads`, after `implement`, and a Role `source` on `implement`."""
    put_role(factory, "source", object_schema(["handoff"], **{"handoff": {"type": "string"}, **fields}))
    put_role(factory, "reader", object_schema(["handoff"], handoff={"type": "string"}), reads)
    factory.pipeline["stages"]["implement"] = {
        "role": "source",
        "inputs": ["spec", "ticket"],
        "next": {"done": "review"},
    }
    factory.pipeline["stages"]["review"] = {"role": "reader", "inputs": inputs, "next": {"done": "catch_up"}}


def test_a_stage_summary_that_is_not_a_string_breaks_the_contract(factory: Harness) -> None:
    put_role(factory, "counter", object_schema(["handoff"], summary={"type": "integer"}, handoff={"type": "string"}))
    factory.pipeline["stages"]["implement"]["role"] = "counter"

    assert errors_of(factory) == ["stages.implement: Role counter: the summary of a Stage must be a string"]


def test_a_describer_schema_must_require_title_and_body(factory: Harness) -> None:
    put_role(factory, "writer", object_schema(["title"], title={"type": "string"}, body={"type": "string"}))
    set_describe(factory, role="writer")

    assert errors_of(factory) == ["nodes.describe: Role writer: the schema of the Describer must require body"]


def test_a_model_decider_outcome_must_be_an_enum_within_the_outcomes_of_the_decision(factory: Harness) -> None:
    put_role(factory, "judge", object_schema(["outcome"], outcome={"enum": ["accept", "reject"]}))
    factory.pipeline["stages"]["implement"]["next"]["done"] = "vote"
    factory.pipeline["decisions"] = {
        "vote": {
            "decider": {"kind": "model", "role": "judge"},
            "outcomes": ["accept"],
            "next": {"accept": "catch_up"},
        }
    }

    assert errors_of(factory) == [
        "decisions.vote: Role judge: the enum of outcome has reject, which is no outcome of the Decision"
    ]


def test_a_model_decider_outcome_that_is_no_enum_breaks_the_contract(factory: Harness) -> None:
    put_role(factory, "judge", object_schema(["outcome"], outcome={"type": "string"}))
    factory.pipeline["stages"]["implement"]["next"]["done"] = "vote"
    factory.pipeline["decisions"] = {
        "vote": {
            "decider": {"kind": "model", "role": "judge"},
            "outcomes": ["accept"],
            "next": {"accept": "catch_up"},
        }
    }

    assert errors_of(factory) == [
        "decisions.vote: Role judge: the schema of a model Decider must require outcome as an enum"
    ]


def test_a_role_on_a_decider_that_is_no_model_is_not_checked_as_the_role_of_a_model_decider(factory: Harness) -> None:
    put_role(factory, "judge", object_schema(["handoff"], handoff={"type": "string"}), body={"words": ["Verdict"]})
    factory.pipeline["stages"]["implement"]["next"]["done"] = "vote"
    factory.pipeline["decisions"] = {
        "vote": {
            "decider": {"kind": "human", "role": "judge"},
            "outcomes": ["accept"],
            "next": {"accept": "catch_up"},
        }
    }

    result = factory.check()

    assert result.code == 0, result.err


def test_a_run_agent_other_than_the_describer_may_not_have_a_role_with_a_body(factory: Harness) -> None:
    schema = object_schema(
        ["summary", "handoff", "failure", "change"],
        summary={"type": "string"},
        handoff={"type": "string"},
        failure={"type": "string"},
        change={"type": "string"},
    )
    put_role(factory, "fixer", schema, body={"words": ["Door"]})
    factory.pipeline["ci_fix"] = {"role": "fixer", "gates": ["build"]}
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {
        "build": {"kind": "command", "with": {"run": "make"}, "next": {"pass": "catch_up", "fail": "abandoned"}}
    }

    assert errors_of(factory) == ["ci_fix: Role fixer: only the Describer reads `body`"]


def risk_after_implement(factory: Harness, **fields: Any) -> None:
    put_role(factory, "source", object_schema(["handoff"], **{"handoff": {"type": "string"}, **fields}))
    factory.pipeline["stages"]["implement"] = {
        "role": "source",
        "inputs": ["spec", "ticket"],
        "next": {"done": "risk"},
    }
    factory.pipeline["decisions"] = {
        "risk": {
            "decider": {"kind": "function", "function": "risk"},
            "outcomes": ["risky", "safe"],
            "inputs": [{"stage": "implement"}],
            "next": {"safe": "catch_up", "risky": "abandoned"},
        }
    }


def test_a_risk_decision_needs_an_input_stage_that_outputs_the_door_and_blast_radius_it_reads(
    factory: Harness,
) -> None:
    risk_after_implement(factory, door={"enum": ["one-way", "two-way"]}, blast_radius={"type": "string"})

    assert errors_of(factory) == [
        "decisions.risk: no input Stage can output door as two_way or one_way"
        " and blast_radius as local or module or cross_module or external"
    ]

    risk_after_implement(
        factory,
        door={"enum": ["two_way", "one_way"]},
        blast_radius={"enum": ["local", "module", "cross_module", "external"]},
    )

    assert factory.check().code == 0


def test_a_risk_decision_whose_inputs_have_no_role_is_not_checked_against_what_it_reads(factory: Harness) -> None:
    risk_after_implement(factory)
    factory.pipeline["decisions"]["risk"]["inputs"] = ["spec"]

    result = factory.check()

    assert result.code == 0, result.err


def test_a_risk_decision_whose_input_role_has_no_schema_reports_only_the_schema(factory: Harness) -> None:
    risk_after_implement(factory)
    (factory.factory_library / "schemas" / "source.json").write_text('{"type": 7}')

    assert errors_of(factory) == [
        "stages.implement: the schema of Role source is not a valid JSON Schema:"
        " 7 is not valid under any of the given schemas"
    ]


def test_a_risk_decision_reading_a_field_defined_as_no_mapping_has_no_input_stage_that_outputs_it(
    factory: Harness,
) -> None:
    risk_after_implement(factory, door=True, blast_radius={"enum": ["local", "module", "cross_module", "external"]})

    assert errors_of(factory) == [
        "decisions.risk: no input Stage can output door as two_way or one_way"
        " and blast_radius as local or module or cross_module or external"
    ]


@pytest.mark.parametrize(
    "definition",
    [{"description": "Anything."}, {"enum": [1, 2]}],
    ids=["no_type_and_no_enum", "enum_of_no_strings"],
)
def test_a_required_read_of_a_field_whose_definition_names_no_type_passes(
    factory: Harness, definition: dict[str, Any]
) -> None:
    reading(factory, {"required": {"extra": "array"}}, [{"stage": "implement"}])
    put_role(factory, "source", object_schema(["handoff", "extra"], handoff={"type": "string"}, extra=definition))

    result = factory.check()

    assert result.code == 0, result.err


def test_a_required_read_from_a_stage_that_outputs_it_passes(factory: Harness) -> None:
    reading(factory, {"required": {"extra": "string"}}, ["spec", {"stage": "implement"}], extra={"type": "string"})
    factory.pipeline["stages"]["implement"]["next"]["done"] = "review"
    put_role(
        factory, "source", object_schema(["handoff", "extra"], handoff={"type": "string"}, extra={"type": "string"})
    )

    assert factory.check().code == 0


def test_a_required_read_with_the_stage_missing_from_inputs_is_an_error(factory: Harness) -> None:
    reading(factory, {"required": {"extra": "string"}}, ["spec", "ticket"], extra={"type": "string"})

    assert errors_of(factory) == [
        "stages.review: Role reader reads required field extra, but no input is the output of a Stage"
    ]


def test_a_required_read_of_a_field_missing_from_the_schema_is_an_error(factory: Harness) -> None:
    reading(factory, {"required": {"extra": "string"}}, [{"stage": "implement"}])

    assert errors_of(factory) == [
        "stages.review: Role reader reads required field extra, "
        "but the schema of Role source of implement has no field extra"
    ]


def test_a_required_read_of_a_field_the_schema_does_not_require_is_an_error(factory: Harness) -> None:
    reading(factory, {"required": {"extra": "string"}}, [{"stage": "implement"}], extra={"type": "string"})

    assert errors_of(factory) == [
        "stages.review: Role reader reads required field extra, but implement does not always output extra"
    ]


def test_a_required_read_from_an_optional_input_is_an_error(factory: Harness) -> None:
    reading(factory, {"required": {"handoff": "string"}}, [{"stage": "implement", "optional": True}])

    assert errors_of(factory) == [
        "stages.review: Role reader reads required field handoff, but input stage implement is optional"
    ]


def test_an_optional_read_that_no_input_provides_passes(factory: Harness) -> None:
    reading(factory, {"optional": {"extra": "string"}}, ["spec", {"stage": "implement"}])

    assert factory.check().code == 0


@pytest.mark.parametrize("presence", ["required", "optional"])
def test_a_read_with_a_different_type_is_an_error(factory: Harness, presence: str) -> None:
    reading(factory, {presence: {"extra": "array"}}, [{"stage": "implement"}], extra={"type": "string"})

    assert errors_of(factory) == [
        f"stages.review: Role reader reads {presence} field extra, but implement outputs extra as string, not array"
    ]


def test_the_describers_required_reads_are_checked_against_the_stages_of_the_pipeline(factory: Harness) -> None:
    put_role(
        factory,
        "writer",
        object_schema(["title", "body"], title={"type": "string"}, body={"type": "string"}),
        {"required": {"findings": "array"}, "optional": {"handoff": "array"}},
    )
    set_describe(factory, role="writer")

    assert errors_of(factory) == [
        "nodes.describe: Role writer reads required field findings, but no Stage outputs it",
        "nodes.describe: Role writer reads handoff, but implement outputs handoff as string, not array",
    ]

    put_role(
        factory,
        "writer",
        object_schema(["title", "body"], title={"type": "string"}, body={"type": "string"}),
        {"required": {"handoff": "string"}},
    )

    result = factory.check()

    assert result.code == 0, result.err


def test_a_reads_block_with_an_unknown_type_is_an_error(factory: Harness) -> None:
    reading(factory, {"optional": {"extra": "text"}}, ["spec"])

    [error] = errors_of(factory)
    assert "role reader in factory: read extra has type 'text'" in error


def test_a_model_decider_with_inputs_checks_its_reads_like_a_stage(factory: Harness) -> None:
    put_role(
        factory,
        "judge",
        object_schema(["outcome"], outcome={"enum": ["accept"]}),
        {"required": {"extra": "string"}},
    )
    factory.pipeline["stages"]["implement"]["next"]["done"] = "vote"
    factory.pipeline["decisions"] = {
        "vote": {
            "decider": {"kind": "model", "role": "judge"},
            "inputs": [{"stage": "implement"}],
            "outcomes": ["accept"],
            "next": {"accept": "catch_up"},
        }
    }

    [error] = errors_of(factory)
    assert error.startswith("decisions.vote: Role judge reads required field extra, but the schema of Role")


def test_check_reports_git_remotes_that_no_kind_of_pull_requests_serves_until_code_host_names_one(
    factory: Harness,
) -> None:
    factory.remove_remotes()
    factory.git("remote", "add", "origin", "git@git.example.com:group/project.git")

    result = factory.check()

    assert result.code == 2
    assert "no kind of PullRequests serves the git remotes" in result.err
    assert "git@git.example.com:group/project.git" in result.err
    assert "code_host" in result.err

    factory.project["code_host"] = "gitlab"

    assert factory.check().code == 0


def test_check_rejects_a_code_host_that_is_no_kind(factory: Harness) -> None:
    factory.project["code_host"] = "bitbucket"

    result = factory.check()

    assert result.code == 2
    assert "code_host: must be one of github, gitlab" in result.err


def test_an_unknown_model_is_an_error_where_it_is_set(factory: Harness) -> None:
    factory.pipeline["defaults"]["model"] = "gpt-4"
    factory.pipeline["stages"]["implement"]["model"] = "sonet"

    assert errors_of(factory) == [
        f"defaults.model: gpt-4 is not a model of the claude-code Executor; {HINT}",
        f"stages.implement.model: sonet is not a model of the claude-code Executor; {HINT}",
    ]


def test_an_unknown_escalate_model_is_an_error(factory: Harness) -> None:
    fix_loop(factory, escalate={"from_round": 2, "model": "bigger"})

    assert errors_of(factory) == [
        f"stages.fix.escalate.model: bigger is not a model of the claude-code Executor; {HINT}"
    ]


def test_an_unknown_decider_model_is_an_error(factory: Harness) -> None:
    factory.pipeline["decisions"] = {
        "triage": {
            "decider": {"kind": "model", "role": "implementer", "model": "nope"},
            "outcomes": ["done"],
            "next": {"done": "merge"},
        }
    }

    assert any(line.startswith("decisions.triage.decider.model: nope is not a model") for line in errors_of(factory))


def test_an_unknown_describer_model_is_an_error(factory: Harness) -> None:
    put_role(factory, "writer", object_schema(["title", "body"], title={"type": "string"}, body={"type": "string"}))
    set_describe(factory, role="writer", model="nope")

    assert errors_of(factory) == [f"nodes.describe.model: nope is not a model of the claude-code Executor; {HINT}"]


def test_full_model_ids_and_aliases_pass(factory: Harness) -> None:
    factory.pipeline["defaults"]["model"] = "claude-sonnet-4-5-20250929"
    factory.pipeline["stages"]["implement"]["model"] = "opus[1m]"

    assert factory.check().code == 0


def test_an_unknown_effort_is_an_error_where_it_is_set(factory: Harness) -> None:
    factory.pipeline["defaults"]["effort"] = "extreme"
    factory.pipeline["stages"]["implement"]["effort"] = 3
    fix_loop(factory, escalate={"from_round": 2, "model": "opus", "effort": "huge"})
    put_role(factory, "writer", object_schema(["title", "body"], title={"type": "string"}, body={"type": "string"}))
    set_describe(factory, role="writer", effort="nope")

    hint = "use low, medium, high, xhigh, max"
    assert sorted(errors_of(factory)) == [
        f"defaults.effort: extreme is not an effort of the claude-code Executor; {hint}",
        f"nodes.describe.effort: nope is not an effort of the claude-code Executor; {hint}",
        f"stages.fix.escalate.effort: huge is not an effort of the claude-code Executor; {hint}",
        "stages.implement.effort: must be a string",
    ]


class PairedEfforts(ScriptedExecutor):
    """PairedEfforts allows the effort max only for the model big, as an Executor whose efforts differ per model."""

    name = "paired"
    default_model = "small"

    def settings_problem(self, model: str, effort: str | None) -> str | None:
        if model not in ("small", "big"):
            return f"{model} is not a model of the paired Executor; use small or big"
        if effort == "max" and model != "big":
            return f"{effort} is not an effort of the model {model}; use max only with big"
        return None


def test_a_pair_of_model_and_effort_is_checked_where_either_is_set(factory: Harness) -> None:
    factory.registries.executors.register("paired", Kind(lambda settings, tools: PairedEfforts()))
    factory.project["executor"] = "paired"
    factory.pipeline["defaults"].update({"model": "big", "effort": "max"})
    factory.pipeline["stages"]["implement"]["model"] = "small"
    fix_loop(factory, escalate={"from_round": 2, "model": "small"})
    put_role(factory, "writer", object_schema(["title", "body"], title={"type": "string"}, body={"type": "string"}))
    set_describe(factory, role="writer", model="small", effort="max")

    assert sorted(errors_of(factory)) == [
        "nodes.describe.effort: max is not an effort of the model small; use max only with big",
        "stages.fix.escalate.model: max is not an effort of the model small; use max only with big",
        "stages.implement.model: max is not an effort of the model small; use max only with big",
    ]


def test_efforts_of_the_executor_pass(factory: Harness) -> None:
    factory.pipeline["defaults"]["effort"] = "medium"
    factory.pipeline["stages"]["implement"]["effort"] = "xhigh"

    assert factory.check().code == 0


def test_operation_of_the_retired_kind_sync_names_catch_up(factory: Harness) -> None:
    factory.pipeline["operations"]["catch_up"]["kind"] = "sync"

    assert errors_of(factory) == ["operations.catch_up: kind sync is now catch_up"]


def test_node_in_kebab_case_fails_and_names_the_node(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = "ruff-check"
    factory.pipeline["gates"] = {
        "ruff-check": {"kind": "command", "with": {"run": "true"}, "next": {"pass": "catch_up", "fail": "abandoned"}}
    }

    assert errors_of(factory) == ["gates.ruff-check: node name ruff-check must match [a-z][a-z0-9_]*"]


def test_name_that_yaml_reads_as_no_string_fails_with_an_error(factory: Harness) -> None:
    factory.pipeline["escalate_to"] = 5
    factory.pipeline["gates"] = {
        6: {"kind": "command", "with": {"run": "true"}, "next": {"pass": "catch_up", "fail": "abandoned"}}
    }

    errors = errors_of(factory)

    assert "gates.6: node name 6 must match [a-z][a-z0-9_]*" in errors
    assert "escalate_to: names undeclared node 5" in errors
