import json
from pathlib import Path

from harness import AgentScript, Harness, run_id_of

REVIEWER = {
    "prompt": "prompts/reviewer.md",
    "output": "schemas/reviewer.json",
    "skills": ["review"],
}

FINDINGS = [
    {
        "id": "F1",
        "axis": "standards",
        "file": "log.txt",
        "line": 1,
        "summary": "The line says `line`, which is no domain word.",
        "outcome": "fixed",
        "reason": "Reworded the line.",
    },
    {
        "id": "F2",
        "axis": "spec",
        "file": "log.txt",
        "line": 1,
        "summary": "The log holds no timestamp.",
        "outcome": "skipped",
        "reason": "The Ticket does not ask for one.",
    },
]
REVIEW = AgentScript(
    run=["echo reviewed >> log.txt"],
    output={
        "summary": "Reword the log line",
        "handoff": "reviewed, one fix",
        "door": "two-way",
        "blast_radius": "local",
        "danger_notes": "",
        "findings": FINDINGS,
    },
)
APPEND = AgentScript(run=["echo line >> log.txt"], output={"summary": "Append a line", "handoff": "appended"})
DESCRIPTION = AgentScript(
    output={
        "title": "Append a line to the log",
        "body": (
            "## Summary\n\nx\n\n## Evidence\n\nx\n\n"
            "## Merge Danger\n\n- **Door**: two-way.\n- **Blast Radius**: local.\n"
        ),
    }
)


def with_review(factory: Harness) -> None:
    factory.pipeline["roles"]["reviewer"] = REVIEWER
    factory.pipeline["stages"]["implement"]["next"] = {"done": "review"}
    factory.pipeline["stages"]["review"] = {
        "role": "reviewer",
        "inputs": ["spec", "ticket"],
        "next": {"done": "sync"},
    }
    factory.pipeline["describer"] = {"role": "describer"}
    factory.executor.scripts["implement"] = APPEND
    factory.executor.scripts["review"] = REVIEW
    factory.executor.scripts["describe"] = DESCRIPTION


def test_green_path_goes_through_review_and_its_fixes_land_in_a_stage_boundary_commit(factory: Harness) -> None:
    with_review(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": "Write hello.txt.\n"})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    subjects = factory.git("log", "--format=%s", f"factory/{run_id}").splitlines()
    assert subjects[:2] == ["Reword the log line", "Append a line"]
    assert factory.git("show", f"factory/{run_id}:log.txt") == "line\nreviewed"


def test_review_output_lists_every_finding_and_reaches_the_describers_brief(factory: Harness) -> None:
    with_review(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": "Write hello.txt.\n"})

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    [started] = factory.events(run_id, "DescriberStarted")
    [review] = [i for i in started["brief"]["inputs"] if i.get("node") == "review"]
    output = json.loads(Path(review["path"]).read_text())
    assert [(f["id"], f["outcome"]) for f in output["findings"]] == [("F1", "fixed"), ("F2", "skipped")]
    assert all(f["reason"] for f in output["findings"])
    assert (output["door"], output["blast_radius"]) == ("two-way", "local")


def test_reviewer_output_without_an_outcome_on_a_finding_fails_the_attempt(factory: Harness) -> None:
    with_review(factory)
    bad = {k: v for k, v in FINDINGS[0].items() if k != "outcome"}
    factory.executor.scripts["review"] = AgentScript(output={**REVIEW.output, "findings": [bad]})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": "Write hello.txt.\n"})

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    assert "'outcome' is a required property" in json.dumps(factory.events(run_id, "AttemptFinished"))
