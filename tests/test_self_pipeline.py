"""The factory's own project file runs Pipeline `self`, which builds the factory with the factory."""

import contextlib
import io
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from factory.cli import main
from factory.config import display_path
from factory.pipeline.pipeline import Gate, Operation, Stage
from factory.pipeline.pipeline_checks import node_name_problem
from factory.run.inputs import schema_errors
from factory.wiring import Machine
from harness import load_real_project

REPO = Path(__file__).resolve().parents[1]
PROJECT = REPO / "factory.yaml"
LIBRARY = REPO / "src" / "factory" / "library"


def test_factory_check_accepts_the_project_file(tmp_path: Path) -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["check", "--project", str(PROJECT)], Machine(home=tmp_path))

    assert code == 0
    assert out.getvalue().splitlines() == [f"{display_path(PROJECT)}: OK"]


def test_self_gates_before_review_and_again_after_catch_up_before_merge() -> None:
    project = load_real_project(PROJECT)
    pipeline = project.pipeline

    assert (project.base, project.repo) == ("main", REPO)
    assert (pipeline.start, pipeline.escalate_to) == ("implement", "abandoned")
    success = {Stage: "done", Gate: "pass"}
    route = ["implement"]
    while route[-1] not in ("done", "abandoned"):
        node = pipeline.nodes[route[-1]]
        route.append(node.next[node.outcomes[0] if isinstance(node, Operation) else success[type(node)]])
    assert route == [
        *("implement", "ruff", "mypy", "pytest", "review", "catch_up"),
        *("ruff_combined", "mypy_combined", "pytest_combined", "merge", "done"),
    ]
    for gate_name in ("ruff", "mypy", "pytest"):
        before, after = pipeline.nodes[gate_name], pipeline.nodes[f"{gate_name}_combined"]
        assert isinstance(before, Gate) and isinstance(after, Gate)
        assert (before.kind, before.next["fail"]) == ("command", "fix")
        assert (after.kind, after.next["fail"]) == ("command", "fix_combined")
        assert before.with_ == after.with_


def test_fix_and_fix_combined_return_to_their_loop_and_run_out_of_rounds_into_abandoned() -> None:
    pipeline = load_real_project(PROJECT).pipeline
    fix = pipeline.nodes["fix"]

    assert isinstance(fix, Stage)
    combined = pipeline.nodes["fix_combined"]
    assert isinstance(combined, Stage)
    assert (fix.next["done"], combined.next["done"]) == ("ruff", "catch_up")
    for stage in (fix, combined):
        assert stage.max_rounds == 3
        assert pipeline.route(stage, "exhausted") == "abandoned"
        assert [stage.settings.for_round(n).model for n in (1, 2, 3)] == ["opus", "opus", "opus"]


def test_fix_gets_the_failed_gate_and_the_handoff_of_every_earlier_round() -> None:
    fix = load_real_project(PROJECT).pipeline.nodes["fix"]

    assert isinstance(fix, Stage)
    assert {i.source for i in fix.inputs} >= {"ticket", "cause"}
    [handoffs] = [i for i in fix.inputs if i.source == "stage"]
    assert (handoffs.nodes, handoffs.all_rounds) == (("implement", "fix"), True)


def test_fix_combined_also_gets_the_output_of_review() -> None:
    combined = load_real_project(PROJECT).pipeline.nodes["fix_combined"]

    assert isinstance(combined, Stage)
    [handoffs] = [i for i in combined.inputs if i.source == "stage"]
    assert (handoffs.nodes, handoffs.all_rounds) == (("implement", "fix", "review", "fix_combined"), True)


def test_implementer_schema_requires_a_summary_and_a_handoff() -> None:
    pipeline = load_real_project(PROJECT).pipeline
    implement = pipeline.nodes["implement"]

    assert isinstance(implement, Stage)
    schema = json.loads(implement.role.output.read_text())
    assert set(schema["required"]) == {"summary", "handoff"}


def test_self_describes_the_final_pr_with_skill_pr() -> None:
    describer = load_real_project(PROJECT).pipeline.describer

    assert describer is not None
    assert (describer.role.name, describer.role.skills, describer.settings.attempts) == ("describer", ("pr",), 2)
    assert {"title", "body"} <= set(json.loads(describer.role.output.read_text())["required"])


def test_self_runs_four_tickets_at_once_and_gives_each_stage_a_second_attempt() -> None:
    project = load_real_project(PROJECT)
    stages = [node for node in project.pipeline.nodes.values() if isinstance(node, Stage)]

    assert project.max_parallel == 4
    assert {stage.name: stage.settings.attempts for stage in stages} == {
        "implement": 2,
        "review": 2,
        "fix": 2,
        "fix_combined": 2,
        "resolve": 2,
    }


def test_self_reviews_on_opus_in_a_workspace_it_may_write_and_reads_the_spec_and_the_ticket() -> None:
    review = load_real_project(PROJECT).pipeline.nodes["review"]

    assert isinstance(review, Stage)
    assert (review.role.name, review.role.skills, review.settings.for_round(1).model, review.commits) == (
        "reviewer",
        ("review",),
        "opus",
        True,
    )
    assert [i.source for i in review.inputs] == ["spec", "ticket"]


FINDING: dict[str, Any] = {
    "id": "F1",
    "axis": "spec",
    "file": "a.py",
    "line": 1,
    "summary": "x",
    "outcome": "fixed",
    "reason": "y",
}
AXES = {"standards": "No findings.", "spec": "One finding."}
REVIEW: dict[str, Any] = {
    "summary": "Reword",
    "handoff": "reviewed",
    "door": "two_way",
    "blast_radius": "local",
    "danger_notes": "",
    "findings": [FINDING],
    "axes": AXES,
}


def test_reviewer_schema_requires_findings_with_an_outcome_the_merge_danger_fields_and_both_axis_reports() -> None:
    review = load_real_project(PROJECT).pipeline.nodes["review"]

    assert isinstance(review, Stage)
    schema = review.role.output
    assert schema_errors(schema, REVIEW) == []
    for field in REVIEW:
        assert schema_errors(schema, {k: v for k, v in REVIEW.items() if k != field}) == [
            f"'{field}' is a required property"
        ]
    for field in FINDING:
        finding = {k: v for k, v in FINDING.items() if k != field}
        assert schema_errors(schema, {**REVIEW, "findings": [finding]}) == [f"'{field}' is a required property"]
    assert schema_errors(schema, {**REVIEW, "findings": [{**FINDING, "outcome": "skipped", "axis": "security"}]}) == []
    assert schema_errors(schema, {**REVIEW, "findings": [{**FINDING, "outcome": "maybe"}]})
    for axis in ("standards", "spec"):
        assert schema_errors(schema, {**REVIEW, "axes": {**AXES, axis: ""}})
        missing = {k: v for k, v in AXES.items() if k != axis}
        assert schema_errors(schema, {**REVIEW, "axes": missing}) == [f"'{axis}' is a required property"]


def test_describer_prompt_names_the_findings_the_reviewer_skipped() -> None:
    describer = load_real_project(PROJECT).pipeline.describer

    assert describer is not None
    assert "skipped" in describer.role.prompt.read_text()


def test_reviewer_prompt_has_the_two_axes_run_by_parallel_sub_agents_and_does_not_judge_them() -> None:
    review = load_real_project(PROJECT).pipeline.nodes["review"]

    assert isinstance(review, Stage)
    prompt = review.role.prompt.read_text()
    assert "two parallel sub-agents" in prompt
    assert "do not judge an axis yourself" in prompt.lower()
    assert "Read `AGENTS.md`" not in prompt


def test_self_gives_each_sandbox_more_than_one_cpu_for_the_pytest_workers() -> None:
    project = load_real_project(PROJECT)
    gate = project.pipeline.nodes["pytest"]

    assert project.sandbox.settings.cpus > 1
    assert isinstance(gate, Gate)
    assert "pytest -n auto" in gate.with_["run"]


def test_implementer_and_reconciler_prompts_run_targeted_tests_and_the_full_suite_once_without_sleep() -> None:
    pipeline = load_real_project(PROJECT).pipeline

    for name in ("implement", "resolve"):
        stage = pipeline.nodes[name]
        assert isinstance(stage, Stage)
        prompt = stage.role.prompt.read_text()
        assert "uv run --locked pytest -n auto" in prompt
        assert "only the tests" in prompt
        assert "once at the end" in prompt
        assert "`sleep`" in prompt


def test_reviewer_prompt_leaves_the_full_pytest_to_the_gate() -> None:
    review = load_real_project(PROJECT).pipeline.nodes["review"]

    assert isinstance(review, Stage)
    prompt = review.role.prompt.read_text()
    assert "- `uv run --locked pytest`" not in prompt
    assert "Gate `pytest_combined`" in prompt


@pytest.mark.parametrize(
    "path", [REPO / ".factory" / "pipelines" / "self.yaml", LIBRARY / "pipelines" / "tdd-review.yaml"]
)
def test_every_node_of_both_pipelines_is_snake_case(path: Path) -> None:
    data = yaml.safe_load(path.read_text())

    names = [name for section in ("stages", "gates", "decisions", "operations") for name in data.get(section, {})]

    assert [problem for name in names if (problem := node_name_problem(name))] == []
