"""A Run resolves the Library once, and its skills reach the agent through the kit the Run assembles (ADR 0010)."""

import json
from pathlib import Path

from factory.ports.executor import AttemptRequest
from harness import AgentScript, Harness, run_id_of, ticket_text
from test_recovery import STALE, attempt_started, crashed_run, killed_runs_run_no_handler  # noqa: F401

TICKET = ticket_text("Greet")
DONE = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})


def put(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def skill(name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {body}\n---\n{body}\n"


def prompt_of(factory: Harness, request: AttemptRequest) -> str:
    argv: list[str] = factory.executor.launch(request)["argv"]
    return argv[argv.index("-p") + 1]


def test_a_prompt_and_a_skill_edited_between_run_and_resume_do_not_reach_the_resumed_run(factory: Harness) -> None:
    factory.pipeline["roles"]["implementer"]["skills"] = ["extra"]
    project = factory.repo / ".factory"
    user = factory.home / "library"
    put(user, "prompts/implementer.md", "Version one.\n")
    put(project, "skills/extra/SKILL.md", skill("extra", "first"))
    stuck = AgentScript(files={"junk.txt": "half\n"}, output=DONE.output, wait_for=factory.root / "go")
    factory.executor.scripts["implement"] = [stuck, DONE]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    run_id = crashed_run(factory, spec, attempt_started(factory))
    put(user, "prompts/implementer.md", "Version two.\n")
    put(project, "skills/extra/SKILL.md", skill("extra", "second"))
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    first, second = factory.executor.requests
    assert prompt_of(factory, first).startswith("Version one.\n")
    assert prompt_of(factory, second).startswith("Version one.\n")
    assert second.prompt.read_text() == first.prompt.read_text()
    assert (second.kit / "plugin" / "skills" / "extra" / "SKILL.md").read_text() == skill("extra", "first")


def fingerprint(factory: Harness, spec: Path) -> dict[str, str]:
    factory.clock.sleep(1)
    result = factory.run(spec)
    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [started] = factory.events(run_id, "AttemptStarted")
    [run] = factory.events(run_id, "RunStarted")
    return {key: started[key] for key in ("prompt_sha", "schema_sha")} | {"kit_sha": run["kit_sha"], "run": run_id}


def test_the_fingerprint_and_the_kit_follow_the_final_prompt_schema_and_skills_of_the_layers(factory: Harness) -> None:
    factory.pipeline["defaults"]["profile"] = "hermetic"
    factory.pipeline["roles"]["implementer"]["skills"] = ["extra"]
    factory.executor.scripts["implement"] = DONE
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    project = factory.repo / ".factory"
    put(project, "skills/extra/SKILL.md", skill("extra", "only in the project"))
    base = fingerprint(factory, spec)

    extension = {"x-extends": "implementer", "properties": {"note": {"type": "string"}}}
    put(project, "schemas/implementer.json", json.dumps(extension))
    schemed = fingerprint(factory, spec)
    put(project, "prompts/implementer.md", "---\nextends: implementer\n---\nBe brief.\n")
    prompted = fingerprint(factory, spec)
    put(project, "prompts/implementer.md", "---\nextends: implementer\n---\nBe thorough.\n")
    put(project, "skills/review/SKILL.md", skill("review", "single axis"))
    skilled = fingerprint(factory, spec)

    assert schemed["schema_sha"] != base["schema_sha"] and schemed["prompt_sha"] == base["prompt_sha"]
    assert prompted["prompt_sha"] != base["prompt_sha"] and prompted["schema_sha"] == schemed["schema_sha"]
    assert skilled["prompt_sha"] not in (base["prompt_sha"], prompted["prompt_sha"])
    assert skilled["schema_sha"] == schemed["schema_sha"]
    assert base["kit_sha"] == schemed["kit_sha"] == prompted["kit_sha"] != skilled["kit_sha"]
    kit = factory.executor.requests[-1].kit
    assert (kit / "plugin" / "skills" / "review" / "SKILL.md").read_text() == skill("review", "single axis")
    assert not (kit / "plugin" / "skills" / "review" / "CREDITS.md").exists()
    assert (kit / "plugin" / "skills" / "extra" / "SKILL.md").exists()
    assert (kit / "plugin" / "skills" / "tdd" / "SKILL.md").exists()
    assert (kit / "plugin" / "hooks" / "no-git-writes.sh").exists()
    frozen = factory.run_dir(skilled["run"]) / "library" / "skills" / "review" / "SKILL.md"
    assert frozen.read_text() == skill("review", "single axis")
