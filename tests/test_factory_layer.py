"""The factory Layer ships the shared Roles, and `self` keeps only its differences in `.factory/`."""

import json
import shutil
from pathlib import Path

from harness import AgentScript, Harness, load_real_project, run_id_of

REPO = Path(__file__).resolve().parents[1]
LIBRARY = REPO / "src" / "factory" / "library"
KIT = REPO / "src" / "factory" / "kit"
TICKET = "---\ntitle: App\n---\nWrite app.py.\n"
REVIEW = AgentScript(
    output={
        "summary": "Found nothing",
        "handoff": "reviewed",
        "door": "two-way",
        "blast_radius": "local",
        "danger_notes": "",
        "findings": [],
    }
)


def single_review_pipeline(factory: Harness) -> None:
    """Points the Harness at a Pipeline of Roles of the factory Layer only, with a one-axis review."""
    shutil.copytree(LIBRARY, factory.factory_library, dirs_exist_ok=True)
    factory.pipeline = {
        "start": "implement",
        "escalate_to": "abandoned",
        "defaults": {"model": "sonnet"},
        "stages": {
            "implement": {"role": "implementer", "inputs": ["spec", "ticket"], "next": {"done": "review"}},
            "review": {"role": "reviewer-single", "inputs": ["spec", "ticket"], "next": {"done": "sync"}},
        },
        "operations": {
            "sync": {"kind": "sync", "next": {"clean": "merge", "conflict": "abandoned"}},
            "merge": {"kind": "merge", "next": {"merged": "done"}},
        },
    }


def test_pipeline_with_a_one_axis_review_is_assembled_from_the_factory_layer_alone(factory: Harness) -> None:
    single_review_pipeline(factory)
    factory.executor.scripts.update(
        implement=AgentScript(files={"app.py": "a = 1\n"}, output={"summary": "Add app.py", "handoff": "done"}),
        review=REVIEW,
    )

    result = factory.run(factory.write_spec("App", "Write an app.", {"t1": TICKET}))

    assert result.code == 0, result.out + result.err
    assert not (factory.repo / ".factory").exists()
    assert factory.git("show", f"factory/{run_id_of(result.out)}:app.py") == "a = 1"
    [request] = [r for r in factory.executor.requests if r.stage == "review"]
    assert "review-single" in request.prompt.read_text()


def test_one_axis_review_output_needs_no_axes(factory: Harness) -> None:
    single_review_pipeline(factory)
    review = factory.pipeline["stages"]["review"]

    assert factory.check().code == 0
    out = factory.cli("check", "--project", str(factory.project_file), "--show", review["role"]).out
    assert "schema reviewer-single: merge-danger from factory, reviewer-single from factory" in out
    assert "axes" not in out.split("schema reviewer-single")[1]


def test_schemas_chain_through_one_base_from_merge_danger_to_reviewer_single_to_reviewer(factory: Harness) -> None:
    shutil.copytree(LIBRARY, factory.factory_library, dirs_exist_ok=True)
    # The harness writes no Roles of its own, so every Role comes from the copied factory Layer.
    factory.pipeline["roles"] = {}
    factory.pipeline["stages"]["implement"]["role"] = "reviewer"

    out = factory.cli("check", "--project", str(factory.project_file), "--show", "reviewer").out

    assert "schema reviewer: merge-danger from factory, reviewer-single from factory, reviewer from factory" in out
    assert "property door from factory" in out and "property axes from factory" in out


def test_kit_has_the_one_axis_review_skill_beside_the_two_axis_one() -> None:
    single = (KIT / "plugin" / "skills" / "review-single" / "SKILL.md").read_text()
    double = (KIT / "plugin" / "skills" / "review" / "SKILL.md").read_text()

    assert single.startswith("---\nname: review-single\n")
    assert "sub-agent" in double and "Do not split it into sub-agents" in single


def test_the_factory_describer_reads_only_the_summary_and_the_diff() -> None:
    prompt = (LIBRARY / "prompts" / "describer.md").read_text()
    reads = (LIBRARY / "roles" / "describer.yaml").read_text()

    assert "findings" not in prompt and "danger_notes" not in prompt
    assert "findings" not in reads and "summary" in reads


def test_self_has_no_pipelines_directory_and_keeps_its_pipeline_in_the_project_layer() -> None:
    project = load_real_project(REPO / "factory.yaml")

    assert not (REPO / "pipelines").exists()
    assert (REPO / ".factory" / "pipelines" / "self.yaml").is_file()
    assert sorted(p.name for p in (REPO / ".factory").iterdir()) == ["pipelines", "prompts", "roles"]
    assert project.pipeline.start == "implement"


def test_self_extends_the_factory_prompts_and_declares_optional_reads_for_its_describer() -> None:
    pipeline = load_real_project(REPO / "factory.yaml").pipeline
    describer = pipeline.describer

    assert describer is not None
    prompt = describer.role.prompt.read_text()
    assert prompt.startswith("You write the description of the pull request")
    assert "skipped" in prompt and "CONTEXT.md" in prompt
    for name in ("implementer", "reconciler", "reviewer"):
        text = (REPO / ".factory" / "prompts" / f"{name}.md").read_text()
        assert text.startswith(f"---\nextends: {name}\n---\n")
    schema = json.loads(describer.role.output.read_text())
    assert schema["required"] == ["title", "body"]
