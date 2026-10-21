import json

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


def test_escalate_to_routes_only_exhausted_and_failed(factory: Harness) -> None:
    del factory.pipeline["escalate_to"]

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

    errors = errors_of(factory)

    assert not [error for error in errors if "missing on some path" in error], errors


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
    factory.pipeline["stages"]["implement"]["next"]["exhausted"] = "abandoned"
    factory.project["overrides"] = {
        "stages": {"implement": {"model": "opus", "inputs": ["ticket"], "next": {"failed": "abandoned"}}}
    }
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})

    assert factory.check().code == 0
    assert factory.run(spec).code == 0
    [request] = factory.executor.requests
    assert request.model == "opus"
    assert list(json.loads(request.brief.read_text())["inputs"]) == ["ticket"]


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
    factory.pipeline["stages"]["implement"]["rounds"] = {"max": 3, "then": "abandoned"}
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {"build": {"kind": "tests", "with": {"run": "make"}, "next": {"pass": "merge"}}}

    assert errors_of(factory) == [
        "stages.implement: rounds is not supported yet",
        "gates.build: kind tests is not supported yet",
        "gates.build: outcome fail has no route",
    ]


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
    factory.pipeline["stages"]["implement"]["next"] = {"done": "merge", "exhausted": "rework"}
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
    factory.project = {"pipeline": "missing.yaml", "sandbox": {"kind": "sbx"}}
    project = display_path(factory.project_file)

    result = factory.check()

    assert result.err.splitlines() == [
        f"factory: {project}: sandbox is not supported yet",
        f"factory: {project}: missing base",
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


def test_gate_refuses_bad_attempts_and_clean(factory: Harness) -> None:
    gate(factory, **{"with": {"run": "make"}, "attempts": 0, "clean": "yes"})

    assert errors_of(factory) == [
        "gates.build.attempts: must be a positive integer",
        "gates.build.clean: must be true or false",
    ]


def test_gate_needs_escalate_to_for_a_lost_probe(factory: Harness) -> None:
    del factory.pipeline["escalate_to"]
    factory.pipeline["stages"]["implement"]["next"].update(exhausted="abandoned", failed="abandoned")
    gate(factory, **{"with": {"run": "make"}})

    assert errors_of(factory) == ["gates.build: a lost Probe escalates, so the Pipeline needs escalate_to"]
