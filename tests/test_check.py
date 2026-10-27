import json
from typing import Any

import pytest

from factory.config import display_path
from harness import AgentScript, Harness

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"


def errors_of(factory: Harness) -> list[str]:
    """Returns the error lines of `factory check` without the program prefix and the pipeline path."""
    result = factory.check()
    assert result.code != 0, result.out
    prefix = f"factory: {display_path(factory.pipeline_file)}: "
    return [line.removeprefix(prefix) for line in result.err.splitlines()]


def review_stage(**input_fields: object) -> dict[str, object]:
    return {"role": "implementer", "inputs": [{"stage": "implement", **input_fields}], "next": {"done": "merge"}}


def test_check_accepts_a_valid_config(factory: Harness) -> None:
    result = factory.check()

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


def test_vars_are_substituted_from_the_project_file(factory: Harness) -> None:
    factory.pipeline["defaults"]["model"] = "${vars.model}"
    factory.pipeline["stages"]["implement"]["model"] = "claude-${vars.size}"
    factory.project["vars"] = {"model": "haiku", "size": "opus"}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})

    assert factory.check().code == 0
    assert factory.run(spec).code == 0
    [request] = factory.executor.requests
    assert request.model == "claude-opus"


def test_unknown_variable_is_an_error(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["model"] = "${vars.model}"
    factory.pipeline["operations"]["merge"]["kind"] = "${vars.merge}"
    factory.project["vars"] = {"other": "x"}

    assert errors_of(factory) == [
        "operations.merge.kind: unknown variable merge",
        "stages.implement.model: unknown variable model",
    ]


def test_overrides_merge_dicts_recursively_and_replace_lists(factory: Harness) -> None:
    del factory.pipeline["escalate_to"]
    factory.project["overrides"] = {
        "stages": {"implement": {"model": "opus", "inputs": ["ticket"], "next": {"failed": "abandoned"}}}
    }
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})

    assert factory.check().code == 0
    assert factory.run(spec).code == 0
    [request] = factory.executor.requests
    assert request.model == "opus"
    assert [i["source"] for i in json.loads(request.brief.read_text())["inputs"]] == ["ticket"]


def test_overrides_cannot_change_roles_or_unknown_nodes(factory: Harness) -> None:
    factory.project["overrides"] = {
        "roles": {"implementer": {"prompt": "other.md"}},
        "stages": {"implement": {"prompt": "other.md"}, "review": {"model": "opus"}},
        "decisions": {"triage": {"decider": {"model": "opus"}}},
    }

    result = factory.check()

    assert result.code != 0
    prefix = f"factory: {display_path(factory.project_file)}: "
    assert [line.removeprefix(prefix) for line in result.err.splitlines()] == [
        "overrides.decisions.triage: there is no such Decision",
        "overrides.roles: roles cannot be overridden, only stages, gates and decisions",
        "overrides.stages.implement.prompt: a Role field cannot be overridden",
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


def test_unknown_role_and_missing_role_files_are_errors(factory: Harness) -> None:
    factory.pipeline["roles"]["reviewer"] = {"prompt": "prompts/reviewer.md", "output": "schemas/broken.json"}
    (factory.config / "schemas" / "broken.json").write_text('{"type": 7}')
    factory.pipeline["stages"]["implement"]["role"] = "tester"

    errors = errors_of(factory)

    assert errors == [
        f"roles.reviewer.prompt: cannot read {factory.config / 'prompts' / 'reviewer.md'}",
        "roles.reviewer.output: not a valid JSON Schema: 7 is not valid under any of the given schemas",
        "stages.implement: unknown Role tester",
    ]


def test_check_reports_features_this_engine_does_not_run_yet(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["executor"] = "codex"
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {"build": {"kind": "diff-limits", "with": {}, "next": {"pass": "merge"}}}

    assert errors_of(factory) == [
        "stages.implement: executor is not supported yet",
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


def test_sandbox_retries_is_set_only_in_defaults(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["sandbox_retries"] = 5

    assert errors_of(factory) == ["stages.implement.sandbox_retries: unknown field"]


def test_sandbox_retries_of_zero_passes(factory: Harness) -> None:
    factory.pipeline["defaults"]["sandbox_retries"] = 0

    assert factory.check().code == 0


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
    factory.pipeline["operations"]["implement"] = {"kind": "sync", "next": {"clean": "done", "conflict": "done"}}
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
    factory.project = {"pipeline": "missing.yaml", "sandbox": {"kind": "docker"}}
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


def test_cause_after_exhausted_is_the_cause_that_entered_the_stage(factory: Harness) -> None:
    fix_loop(factory, rounds={"max": 3, "then": "ruling"})
    factory.pipeline["stages"]["ruling"] = {"role": "implementer", "inputs": ["cause"], "next": {"done": "merge"}}

    assert factory.check().code == 0

    factory.pipeline["stages"]["implement"]["rounds"] = {"max": 1, "then": "fix"}

    assert errors_of(factory) == [
        "stages.fix: input cause can be missing on some path",
        "stages.ruling: input cause can be missing on some path",
    ]


def test_parallel_tickets_need_a_sync_on_every_way_into_merge(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["operations"]["sync"]["next"]["clean"] = "build"
    factory.pipeline["gates"] = {
        "build": {"kind": "command", "with": {"run": "true"}, "next": {"pass": "merge", "fail": "abandoned"}}
    }

    result = factory.check()

    assert result.code == 2
    assert result.err.splitlines() == [
        f"factory: {display_path(factory.project_file)}: max_parallel: 2 Tickets at once need a sync before merge, "
        "but stages.implement reaches operations.merge without one"
    ]


def test_one_ticket_at_a_time_merges_without_sync(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = "merge"
    del factory.pipeline["operations"]["sync"]

    result = factory.check()

    assert result.code == 0, result.err


@pytest.mark.parametrize(
    ("target", "body"),
    [
        ("look", {"role": "implementer", "inputs": ["spec", "ticket"], "workspace": "ro", "next": {"done": "sync"}}),
        ("smoke", {"kind": "command", "with": {"run": "true"}, "next": {"pass": "sync", "fail": "abandoned"}}),
    ],
)
def test_sync_conflict_must_go_to_a_stage_that_commits(factory: Harness, target: str, body: dict[str, object]) -> None:
    section = "stages" if "role" in body else "gates"
    factory.pipeline.setdefault(section, {})[target] = body
    factory.pipeline["operations"]["sync"]["next"]["conflict"] = target

    assert errors_of(factory) == [
        f"operations.sync: outcome conflict goes to {target}, "
        "but only a rw Stage can finish the merge that sync leaves in the Workspace"
    ]
