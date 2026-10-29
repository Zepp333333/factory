"""The factory's own project file runs Pipeline `self`, which builds the factory with the factory."""

import contextlib
import io
import json
from pathlib import Path

from factory.cli import Machine, main
from factory.config import display_path
from factory.pipeline import Gate, Operation, Stage
from harness import load_real_project

REPO = Path(__file__).resolve().parents[1]
PROJECT = REPO / "factory.yaml"


def test_factory_check_accepts_the_project_file(tmp_path: Path) -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["check", "--project", str(PROJECT)], Machine(home=tmp_path))

    assert code == 0
    assert out.getvalue().splitlines() == [f"{display_path(PROJECT)}: OK"]


def test_self_gates_before_review_and_again_after_sync_before_merge() -> None:
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
        *("implement", "ruff", "mypy", "pytest", "review", "sync"),
        *("ruff-synced", "mypy-synced", "pytest-synced", "merge", "done"),
    ]
    for gate_name in ("ruff", "mypy", "pytest"):
        before, after = pipeline.nodes[gate_name], pipeline.nodes[f"{gate_name}-synced"]
        assert isinstance(before, Gate) and isinstance(after, Gate)
        assert (before.kind, before.next["fail"]) == ("command", "fix")
        assert (after.kind, after.next["fail"]) == ("command", "fix-synced")
        assert before.with_ == after.with_


def test_fix_and_fix_synced_return_to_their_loop_and_run_out_of_rounds_into_abandoned() -> None:
    pipeline = load_real_project(PROJECT).pipeline
    fix = pipeline.nodes["fix"]

    assert isinstance(fix, Stage)
    synced = pipeline.nodes["fix-synced"]
    assert isinstance(synced, Stage)
    assert (fix.next["done"], synced.next["done"]) == ("ruff", "sync")
    for stage in (fix, synced):
        assert stage.max_rounds == 3
        assert pipeline.route(stage, "exhausted") == "abandoned"
        assert [stage.model_for(n) for n in (1, 2, 3)] == ["sonnet", "opus", "opus"]


def test_fix_gets_the_failed_gate_and_the_handoff_of_every_earlier_round() -> None:
    fix = load_real_project(PROJECT).pipeline.nodes["fix"]

    assert isinstance(fix, Stage)
    assert {i.source for i in fix.inputs} >= {"ticket", "cause"}
    [handoffs] = [i for i in fix.inputs if i.source == "stage"]
    assert (handoffs.nodes, handoffs.all_rounds) == (("implement", "fix"), True)


def test_fix_synced_also_gets_the_output_of_review() -> None:
    synced = load_real_project(PROJECT).pipeline.nodes["fix-synced"]

    assert isinstance(synced, Stage)
    [handoffs] = [i for i in synced.inputs if i.source == "stage"]
    assert (handoffs.nodes, handoffs.all_rounds) == (("implement", "fix", "review", "fix-synced"), True)


def test_implementer_schema_requires_a_summary_and_a_handoff() -> None:
    pipeline = load_real_project(PROJECT).pipeline
    implement = pipeline.nodes["implement"]

    assert isinstance(implement, Stage)
    schema = json.loads(implement.role.output.read_text())
    assert set(schema["required"]) == {"summary", "handoff"}


def test_self_describes_the_final_pr_with_skill_pr() -> None:
    describer = load_real_project(PROJECT).pipeline.describer

    assert describer is not None
    assert (describer.role.name, describer.role.skills, describer.attempts) == ("describer", ("pr",), 2)
    assert {"title", "body"} <= set(json.loads(describer.role.output.read_text())["required"])


def test_self_runs_four_tickets_at_once_and_gives_each_stage_a_second_attempt() -> None:
    project = load_real_project(PROJECT)
    stages = [node for node in project.pipeline.nodes.values() if isinstance(node, Stage)]

    assert project.max_parallel == 4
    assert {stage.name: stage.attempts for stage in stages} == {
        "implement": 2,
        "review": 2,
        "fix": 2,
        "fix-synced": 2,
        "resolve": 2,
    }


def test_self_reviews_on_opus_in_a_workspace_it_may_write_and_reads_the_spec_and_the_ticket() -> None:
    review = load_real_project(PROJECT).pipeline.nodes["review"]

    assert isinstance(review, Stage)
    assert (review.role.name, review.role.skills, review.model_for(1), review.commits) == (
        "reviewer",
        ("review",),
        "opus",
        True,
    )
    assert [i.source for i in review.inputs] == ["spec", "ticket"]


def test_reviewer_schema_requires_findings_with_an_outcome_the_merge_danger_fields_and_both_axis_reports() -> None:
    review = load_real_project(PROJECT).pipeline.nodes["review"]

    assert isinstance(review, Stage)
    schema = json.loads(review.role.output.read_text())
    assert set(schema["required"]) == {"summary", "handoff", "door", "blast_radius", "danger_notes", "findings", "axes"}
    axes = schema["properties"]["axes"]
    assert set(axes["required"]) == {"standards", "spec"}
    assert all(axes["properties"][axis]["minLength"] == 1 for axis in ("standards", "spec"))
    finding = schema["properties"]["findings"]["items"]
    assert set(finding["required"]) == {"id", "axis", "file", "line", "summary", "outcome", "reason"}
    assert finding["properties"]["outcome"]["enum"] == ["fixed", "skipped"]
    assert finding["properties"]["axis"]["enum"] == ["standards", "spec"]


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

    assert project.sandbox.cpus > 1
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
    assert "Gate `pytest-synced`" in prompt
