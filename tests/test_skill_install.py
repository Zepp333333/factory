"""`factory skill install` puts the skill `factory-config` into a Target project for the owner's agent."""

import re
from pathlib import Path

from factory.pipeline.gates import KINDS as GATE_KINDS
from factory.pipeline.pipeline import OPERATION_OUTCOMES, RUN_NODE_KINDS
from factory.wiring import AGENT_KIT, OWNER_SKILLS
from harness import Harness
from test_factory_layer import single_review_pipeline

SKILL = OWNER_SKILLS / "factory-config" / "SKILL.md"


def install(factory: Harness) -> str:
    result = factory.cli("skill", "install", "--project", str(factory.project_file))
    assert result.code == 0, result.err
    return result.out


def installed(factory: Harness) -> Path:
    return factory.repo / ".claude" / "skills" / "factory-config"


def test_install_puts_the_skill_into_the_target_project_and_a_second_run_updates_it(factory: Harness) -> None:
    assert install(factory) == f"Installed skill factory-config in {installed(factory)}\n"
    assert (installed(factory) / "SKILL.md").read_text() == SKILL.read_text()

    (installed(factory) / "SKILL.md").write_text("old\n")
    (installed(factory) / "gone.md").write_text("from an older factory\n")

    assert install(factory) == f"Updated skill factory-config in {installed(factory)}\n"
    assert (installed(factory) / "SKILL.md").read_text() == SKILL.read_text()
    assert not (installed(factory) / "gone.md").exists()


def test_the_skill_is_the_owners_and_no_agent_of_a_run_gets_it() -> None:
    assert "factory-config" not in AGENT_KIT.skills
    assert SKILL.read_text().startswith("---\nname: factory-config\ndescription: ")


def test_the_skill_works_in_its_order() -> None:
    text = SKILL.read_text().partition("Work in this order")[2]
    steps = re.findall(r"^(\d)\. \*\*(.+?)\*\*", text, re.MULTILINE)
    assert [number for number, _ in steps] == ["1", "2", "3", "4", "5"]
    in_order = [
        "Gates",
        "factory library show",
        "`vars` and `overrides`",
        "factory diagram --write",
        "factory check",
        "each branch",
    ]
    places = [text.index(words) for words in in_order]
    assert places == sorted(places)


def test_the_skill_names_no_node_kind_and_points_at_the_guide() -> None:
    text = SKILL.read_text()
    kinds = {*RUN_NODE_KINDS, *OPERATION_OUTCOMES, *GATE_KINDS}
    assert [kind for kind in sorted(kinds) if f"`{kind}`" in text] == []
    assert "docs/guide/04-pipelines.md" in text and "docs/guide/05-customizing.md" in text


def test_the_steps_of_the_skill_give_a_run_graph_that_waits_for_approval_and_merges(factory: Harness) -> None:
    """A scripted owner's agent follows the steps of the skill for the Run graph the Ticket asks about."""
    project = ("--project", str(factory.project_file))
    assert factory.cli("skill", "install", *project).code == 0
    shown = factory.cli("library", "show", "run", "approve-then-merge", *project)
    assert shown.code == 0, shown.err
    path = factory.repo / ".factory" / "runs" / "wait-and-merge.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(shown.out.replace("timeout: 3d", "timeout: 5d"))
    factory.project["run"] = "wait-and-merge"
    single_review_pipeline(factory)

    assert factory.cli("check", *project).code == 2

    assert factory.cli("diagram", "--write", str(path), *project).code == 0

    check = factory.cli("check", *project)
    assert check.code == 0, check.err
    assert path.read_text().startswith("# factory-diagram: begin")
    printed = factory.cli("diagram", str(path), *project).out
    assert 'approval(("approval<br/>wait_tracker<br/>timeout: 5d"))' in printed
    assert '-->|"approved"| land' in printed
