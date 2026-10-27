"""The template Pipeline `tdd-review` passes `factory check` and its green path runs through the CLI."""

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

from factory.config import load_project
from factory.pipeline import Decision, Stage
from harness import AgentScript, FakeTty, Harness, run_id_of

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "pipelines" / "tdd-review"
KIT = REPO / "src" / "factory" / "kit"
ROLES = ("implementer", "reviewer", "triager", "acceptor", "ruler", "resolver")

JUNIT = 'printf \'<testsuite tests="1"><testcase classname="a" name="b"/></testsuite>\' > "$GATE_OUT/junit.xml"'
LCOV = 'printf \'SF:%s/app.py\\nDA:1,1\\nDA:2,1\\nend_of_record\\n\' "$PWD" > "$GATE_OUT/lcov.info"'
VARS = {
    "build": "true",
    "test": f"{JUNIT} && {LCOV}",
    "junit": "junit.xml",
    "coverage_format": "lcov",
    "coverage_report": "lcov.info",
    "scope": "true",
}
TICKET = "---\ntitle: App\n---\nWrite app.py.\n"
IMPLEMENT = AgentScript(files={"app.py": "a = 1\nb = 2\n"}, output={"summary": "Add app.py", "handoff": "done"})
REVIEW = AgentScript(
    output={
        "summary": "Found nothing",
        "handoff": "reviewed",
        "door": "two_way",
        "blast_radius": "local",
        "danger_notes": "",
        "findings": [],
        "axes": {"standards": "No findings.", "spec": "No findings."},
    }
)
ACCEPT = AgentScript(output={"outcome": "accept", "reasons": ["the criterion is met"]})


def use_template(factory: Harness, **project: Any) -> None:
    factory.project.update({"pipeline": str(TEMPLATE / "pipeline.yaml"), "vars": VARS, **project})
    factory.pipeline_file.unlink()


def test_template_with_the_example_project_file_passes_factory_check(factory: Harness) -> None:
    example = yaml.safe_load((TEMPLATE / "factory.example.yaml").read_text())
    use_template(factory, **{k: example[k] for k in ("release", "vars", "sandbox", "max_parallel")})

    result = factory.check()

    assert result.code == 0, result.err


def test_template_gives_every_role_a_prompt_and_an_output_schema() -> None:
    project = load_project(TEMPLATE / "factory.example.yaml")
    nodes = project.pipeline.nodes
    roles = {n.role for n in nodes.values() if isinstance(n, Stage)}
    roles |= {n.decider.role for n in nodes.values() if isinstance(n, Decision) and n.decider.role}

    assert {role.name for role in roles} == set(ROLES)
    for role in roles:
        assert role.prompt.read_text().strip()
        assert json.loads(role.output.read_text())["type"] == "object"


def test_template_sends_exhausted_fix_to_ruling_and_a_conflict_to_resolve_then_review_and_risk_then_sync() -> None:
    pipeline = load_project(TEMPLATE / "factory.example.yaml").pipeline
    fix, sync = pipeline.nodes["fix"], pipeline.nodes["sync"]

    assert pipeline.route(fix, "exhausted") == "ruling"
    assert pipeline.route(sync, "conflict") == "resolve"
    chain = ["resolve"]
    while chain[-1] != "sync":
        node = pipeline.nodes[chain[-1]]
        chain.append(node.next["done" if isinstance(node, Stage) else "safe"])
    assert chain == ["resolve", "review_resolved", "risk_resolved", "sync"]


def test_project_file_declares_the_release_and_check_refuses_another(factory: Harness) -> None:
    factory.project["release"] = "sideload"

    result = factory.check()

    assert result.code != 0
    assert "release: must be one of redeploy, shipped_binary" in result.err


def test_brief_carries_the_release_for_the_reviewer_to_judge_the_door_by(factory: Harness) -> None:
    use_template(factory, release="shipped_binary")
    factory.executor.scripts.update(implement=IMPLEMENT, review=REVIEW, triage=ACCEPT, acceptance=ACCEPT)

    result = factory.run(factory.write_spec("App", "Write an app.", {"t1": TICKET}))

    assert result.code == 0, result.out + result.err
    [request] = [r for r in factory.executor.requests if r.stage == "review"]
    assert json.loads(request.brief.read_text())["release"] == "shipped_binary"
    prompt = (TEMPLATE / "prompts" / "reviewer.md").read_text()
    assert "`release`" in prompt and "shipped_binary" in prompt


def test_green_path_goes_through_review_triage_risk_and_acceptance_and_merges_after_the_gates_on_the_synced_tree(
    factory: Harness,
) -> None:
    use_template(factory, release="redeploy")
    factory.executor.scripts.update(implement=IMPLEMENT, review=REVIEW, triage=ACCEPT, acceptance=ACCEPT)

    result = factory.run(factory.write_spec("App", "Write an app.", {"t1": TICKET}))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.git("show", f"factory/{run_id}:app.py") == "a = 1\nb = 2"
    gates = [e["node"] for e in factory.events(run_id, "GateFinished")]
    assert gates == ["build", "tests", "coverage", "scope", "synced_build", "synced_tests", "synced_coverage"]
    assert [(e["node"], e["outcome"]) for e in factory.events(run_id, "DecisionDecided")] == [
        ("triage", "accept"),
        ("risk", "safe"),
        ("acceptance", "accept"),
    ]
    assert [r.stage for r in factory.executor.requests] == ["implement", "review", "triage", "acceptance"]


def test_kit_has_the_tdd_skill_hooks_and_settings() -> None:
    skill = (KIT / "plugin" / "skills" / "tdd" / "SKILL.md").read_text()
    hooks = json.loads((KIT / "plugin" / "hooks" / "hooks.json").read_text())
    command = hooks["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    assert skill.startswith("---\nname: tdd\n")
    assert (KIT / "plugin" / "hooks" / Path(command).name).exists()
    assert json.loads((KIT / "settings.json").read_text())["autoMemoryEnabled"] is False


def test_hook_blocks_a_git_write_and_lets_a_read_through() -> None:
    script = KIT / "plugin" / "hooks" / "no-git-writes.sh"

    def run(command: str) -> int:
        call = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        return subprocess.run([str(script)], input=call, text=True, capture_output=True).returncode

    assert run("git commit -m x") == 2
    assert run("cd a && git -C b reset --hard") == 2
    assert run("git diff base...HEAD") == 0
    assert run("git log --oneline") == 0
    assert run("git merge-base main HEAD") == 0


FORBIDDEN_TOOLS = ["ScheduleWakeup", "CronCreate", "CronDelete", "CronList", "Monitor"]


def test_kit_denies_the_tools_that_outlive_a_turn_and_builtin_pins_them_absent() -> None:
    settings = json.loads((KIT / "settings.json").read_text())

    assert settings["permissions"]["deny"] == FORBIDDEN_TOOLS
    assert json.loads((KIT / "builtin.json").read_text())["absent_tools"] == FORBIDDEN_TOOLS
    assert settings["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_reviewer_prompt_waits_for_both_axis_reports_before_the_output() -> None:
    prompt = (TEMPLATE / "prompts" / "reviewer.md").read_text()
    fixer = (REPO / "pipelines" / "self" / "prompts" / "reviewer.md").read_text()

    assert "reports of both axes before you call `StructuredOutput`" in prompt
    assert "reports of both axes before you fix anything" in fixer


def test_reviewer_output_without_an_axis_report_fails_the_attempt(factory: Harness) -> None:
    use_template(factory, release="redeploy")
    tty = factory.stdin = FakeTty()
    tty.type("abandon\n")
    factory.executor.scripts.update(
        implement=IMPLEMENT,
        review=AgentScript(output={**REVIEW.output, "axes": {"standards": "ok"}}),
    )

    result = factory.run(factory.write_spec("App", "Write an app.", {"t1": TICKET}))

    tty.close()
    review = [e for e in factory.events(run_id_of(result.out), "AttemptFinished") if e["node"] == "review"]
    assert review and all(e["outcome"] == "failed" for e in review)
    assert "'spec' is a required property" in json.dumps(review)
