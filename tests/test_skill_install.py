"""`factory skill install` puts the skill `factory-config` into a Target project for the owner's agent."""

import re
from pathlib import Path

import pytest

from factory.pipeline.gates import KINDS as GATE_KINDS
from factory.pipeline.pipeline import OPERATION_OUTCOMES
from factory.wiring import AGENT_KIT, OWNER_SKILLS, RUN_NODES
from harness import Harness, git
from test_factory_layer import single_review_pipeline

SKILL = OWNER_SKILLS / "factory-config" / "SKILL.md"


def install(factory: Harness) -> str:
    result = factory.cli("skill", "install", "--project", str(factory.project_file))
    assert result.code == 0, result.err
    return result.out


def installed(factory: Harness) -> Path:
    return factory.repo / ".claude" / "skills" / "factory-config"


def test_install_puts_the_skill_into_the_target_project_and_a_second_run_updates_it(factory: Harness) -> None:
    assert f"Installed skill factory-config in {installed(factory)}\n" in install(factory)
    assert (installed(factory) / "SKILL.md").read_text() == SKILL.read_text()
    setup = factory.repo / ".claude" / "skills" / "factory-setup"
    assert (setup / "SKILL.md").read_text() == SETUP.read_text()

    (installed(factory) / "SKILL.md").write_text("old\n")
    (installed(factory) / "gone.md").write_text("from an older factory\n")

    assert f"Updated skill factory-config in {installed(factory)}\n" in install(factory)
    assert (installed(factory) / "SKILL.md").read_text() == SKILL.read_text()
    assert not (installed(factory) / "gone.md").exists()


def test_the_skill_is_the_owners_and_no_kit_skill() -> None:
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
    kinds = {*RUN_NODES, *OPERATION_OUTCOMES, *GATE_KINDS}
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


SETUP = OWNER_SKILLS / "factory-setup" / "SKILL.md"


def test_install_user_puts_the_setup_skill_into_the_home_of_the_owner_and_a_second_run_updates_it(
    factory: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    skill = tmp_path / ".claude" / "skills" / "factory-setup"

    first = factory.cli("skill", "install", "--user")

    assert first.code == 0, first.err
    assert f"Installed skill factory-setup in {skill}\n" in first.out
    assert (skill / "SKILL.md").read_text() == SETUP.read_text()
    assert not (factory.repo / ".claude").exists()

    (skill / "SKILL.md").write_text("old\n")

    second = factory.cli("skill", "install", "--user")

    assert f"Updated skill factory-setup in {skill}\n" in second.out
    assert (skill / "SKILL.md").read_text() == SETUP.read_text()


def test_the_setup_skill_works_in_its_order_and_keeps_the_gates_as_they_are() -> None:
    text = SETUP.read_text()
    assert text.startswith("---\nname: factory-setup\ndescription: ")
    steps = text.partition("Work in this order")[2]
    assert re.findall(r"^(\d)\. \*\*", steps, re.MULTILINE) == ["1", "2", "3", "4", "5", "6"]
    in_order = ["factory doctor", "`sbx login`", "factory init", "factory check", "doctor --gates", "Do not loosen"]
    in_order += ["after the owner agrees", "trial Spec", "`factory-config`"]
    places = [steps.index(words) for words in in_order]
    assert places == sorted(places)


def test_the_steps_of_the_setup_skill_end_with_a_project_file_and_a_trial_spec_on_a_fake_tracker(
    factory: Harness,
) -> None:
    """A scripted owner's agent follows the steps of the skill in a fresh Python repository on a fake GitHub."""
    project = ("--project", str(factory.project_file))
    factory.leave_project_file()
    (factory.repo / "pyproject.toml").write_text("[project]\nname = 'target'\nversion = '1'\n")
    (factory.repo / "uv.lock").write_text("version = 1\n")
    (factory.repo / "tests.sh").write_text(
        'echo \'<testsuite><testcase classname="a" name="b"/></testsuite>\' > "$GATE_OUT/junit.xml"\n'
        'printf "SF:a.py\\nDA:1,1\\nend_of_record\\n" > "$GATE_OUT/lcov.info"\n'
    )
    git(factory.repo, "add", "-A")
    git(factory.repo, "commit", "-q", "-m", "a project")
    git(factory.repo, "push", "-q", "origin", "main")

    # 1. The first check finds no project file and names what writes it.
    first = factory.cli("doctor", *project)
    assert first.code == 1
    assert "fix: factory init" in first.out

    # 2. The agent writes the project file and fills in what detection missed.
    init = factory.cli("init", "--yes", *project)
    assert init.code == 0, init.err
    text = factory.project_file.read_text()
    assert "sandbox:\n  kind: sbx" in text
    factory.project_file.write_text(
        re.sub(
            r"(?m)^  (build|test): .*$",
            lambda line: f"  {line[1]}: GATE_OUT=$GATE_OUT sh tests.sh  # from tests.sh",
            text,
        )
    )
    checked = factory.cli("check", *project)
    assert checked.code == 0, checked.err

    # 3. The Gates pass on the base branch.
    gates = factory.cli("doctor", "--gates", *project)
    assert gates.code == 0, gates.out
    assert "ok    Gate tests on main" in gates.out

    # 4. The shipped `default` Run graph selects by no label, so the first check named none to create.
    assert "label " not in first.out + gates.out

    # 5. The agent files a trial Spec with one small Ticket on the Tracker and checks it.
    spec = factory.gh.issue(2, "Trial", "Prove the setup", sub_issues=[3])
    factory.gh.issue(3, "Add a line to the README", "Add a line.")
    trial = factory.cli("check", spec, *project)
    assert trial.code == 0, trial.err
