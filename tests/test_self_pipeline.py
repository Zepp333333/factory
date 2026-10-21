"""The factory's own project file runs Pipeline `self`, which builds the factory with the factory."""

import contextlib
import io
import json
from pathlib import Path

from factory.cli import Machine, main
from factory.config import display_path, load_project
from factory.pipeline import Gate, Operation, Stage

REPO = Path(__file__).resolve().parents[1]
PROJECT = REPO / "factory.yaml"


def test_factory_check_accepts_the_project_file(tmp_path: Path) -> None:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(["check", "--project", str(PROJECT)], Machine(home=tmp_path))

    assert code == 0
    assert out.getvalue().splitlines() == [f"{display_path(PROJECT)}: OK"]


def test_self_implements_then_gates_ruff_mypy_and_pytest_before_merge() -> None:
    project = load_project(PROJECT)
    pipeline = project.pipeline

    assert (project.base, project.repo) == ("main", REPO)
    assert (pipeline.start, pipeline.escalate_to) == ("implement", "abandoned")
    success = {Stage: "done", Gate: "pass", Operation: "merged"}
    route = ["implement"]
    while route[-1] not in ("done", "abandoned"):
        node = pipeline.nodes[route[-1]]
        route.append(node.next[success[type(node)]])
    assert route == ["implement", "ruff", "mypy", "pytest", "merge", "done"]
    for name in ("ruff", "mypy", "pytest"):
        gate = pipeline.nodes[name]
        assert isinstance(gate, Gate)
        assert (gate.kind, gate.next["fail"]) == ("command", "fix")


def test_fix_runs_out_of_rounds_into_abandoned() -> None:
    pipeline = load_project(PROJECT).pipeline
    fix = pipeline.nodes["fix"]

    assert isinstance(fix, Stage)
    assert fix.next["done"] == "ruff"
    assert fix.max_rounds == 3
    assert pipeline.route(fix, "exhausted") == "abandoned"
    assert [fix.model_for(n) for n in (1, 2, 3)] == ["sonnet", "opus", "opus"]


def test_fix_gets_the_failed_gate_and_the_handoff_of_every_earlier_round() -> None:
    fix = load_project(PROJECT).pipeline.nodes["fix"]

    assert isinstance(fix, Stage)
    assert {i.source for i in fix.inputs} >= {"ticket", "cause"}
    [handoffs] = [i for i in fix.inputs if i.source == "stage"]
    assert (handoffs.nodes, handoffs.all_rounds) == (("implement", "fix"), True)


def test_implementer_schema_requires_a_summary_and_a_handoff() -> None:
    pipeline = load_project(PROJECT).pipeline
    implement = pipeline.nodes["implement"]

    assert isinstance(implement, Stage)
    schema = json.loads(implement.role.output.read_text())
    assert set(schema["required"]) == {"summary", "handoff"}
