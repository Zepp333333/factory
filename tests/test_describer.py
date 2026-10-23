import json
from pathlib import Path

from harness import AgentScript, Harness, run_id_of

APPEND = AgentScript(run=["echo line >> log.txt"], output={"summary": "Append a line", "handoff": "appended"})

BODY = (
    "## Summary\n\n```diff\n+line\n```\n\n"
    "## Evidence\n\nBefore: no log.txt. After: log.txt holds a line.\n\n"
    "## Merge Danger\n\n- **Door**: two-way, a revert undoes it.\n- **Blast Radius**: single-file, log.txt.\n"
)
DESCRIPTION = AgentScript(output={"title": "Append a line to the log", "body": BODY})


def with_describer(factory: Harness, **fields: object) -> None:
    factory.pipeline["describer"] = {"role": "describer", **fields}
    factory.executor.scripts["implement"] = APPEND
    factory.executor.scripts["describe"] = DESCRIPTION


def publish(factory: Harness) -> str:
    factory.gh.issue(28, "Greet", "Write hello.txt.")
    factory.gh.issue(30, "Greet again", "Write bye.txt.", blocked_by=[28])
    return factory.gh.issue(21, "Greeting", "Say hello.", sub_issues=[28, 30])


def test_pr_gets_the_title_and_sections_the_describer_wrote_and_the_engines_closes_lines(factory: Harness) -> None:
    with_describer(factory)

    result = factory.run(publish(factory))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [pull] = factory.gh.pulls()
    assert pull["title"] == "Append a line to the log"
    assert pull["body"] == f"{BODY}\nCloses #28\nCloses #30\n"
    assert (factory.run_dir(run_id) / "pull_request.md").read_text() == pull["body"]
    assert [event["title"] for event in factory.events(run_id, "PullRequestDescribed")] == ["Append a line to the log"]
    assert factory.events(run_id, "DescriberFailed") == []


def test_only_the_engines_footer_closes_issues_whatever_the_describer_wrote(factory: Harness) -> None:
    factory.gh.issue(28, "Greet", "Write hello.txt.")
    factory.gh.issue(30, "Greet again", "Write bye.txt.", blocked_by=[28])
    factory.gh.issue(31, "Wave", "Write wave.txt.")
    url = factory.gh.issue(21, "Greeting", "Say hello.", sub_issues=[28, 30, 31])
    with_describer(factory)
    factory.executor.scripts["describe"] = AgentScript(
        output={"title": "Wave", "body": BODY + "\nCloses #28, fixes: #99, resolved owner/repo#5, prefixes #7\n"}
    )
    factory.pipeline["defaults"]["sandbox_retries"] = 0
    factory.sandbox.refuse = 1

    result = factory.run(url)

    run_id = run_id_of(result.out)
    [pull] = factory.gh.pulls()
    assert pull["body"].endswith(
        "Refs #28, Refs: #99, Refs owner/repo#5, prefixes #7\n\n"
        "Closes #31\n\n"
        "Not landed:\n\n"
        f"- #28 Greet: abandoned, its work is in `refs/factory/{run_id}/abandoned/28`\n"
        "- #30 Greet again: not started, blocked by #28\n"
    )


def test_describer_reads_a_brief_of_the_spec_merged_tickets_stage_outputs_base_and_integration_branch(
    factory: Harness,
) -> None:
    with_describer(factory)

    result = factory.run(publish(factory))

    run_id = run_id_of(result.out)
    [started] = factory.events(run_id, "DescriberStarted")
    brief = started["brief"]
    base = factory.git("rev-parse", "main")
    assert (brief["base"], brief["base_ref"], brief["integration_branch"]) == (base, "main", f"factory/{run_id}")
    run_dir = factory.run_dir(run_id)
    sources = [(entry["source"], entry.get("ticket"), entry.get("node")) for entry in brief["inputs"]]
    assert sources == [
        ("spec", None, None),
        ("ticket", "28", None),
        ("stage", "28", "implement"),
        ("ticket", "30", None),
        ("stage", "30", "implement"),
    ]
    assert brief["inputs"][0]["path"] == str(run_dir / "spec.md")
    assert json.loads(Path(brief["inputs"][2]["path"]).read_text())["handoff"] == "appended"
    request = factory.executor.requests[-1]
    assert (request.stage, request.model) == ("describe", "sonnet")


def test_describer_works_on_a_workspace_that_is_gone_afterwards_and_its_edits_are_discarded(factory: Harness) -> None:
    with_describer(factory)
    factory.executor.scripts["describe"] = AgentScript(
        files={"junk.txt": "scribbles\n"}, output={"title": "Append a line", "body": BODY}
    )
    before = factory.branches()

    result = factory.run(publish(factory))

    run_id = run_id_of(result.out)
    assert not factory.workspace(run_id, "describe").exists()
    assert set(factory.branches()) == set(before) | {f"factory/{run_id}"}
    assert "junk.txt" in (factory.run_dir(run_id) / "describer" / "attempt-1" / "discarded.patch").read_text()
    assert factory.git("ls-tree", "-r", "--name-only", f"factory/{run_id}").splitlines() == [
        "README.md",
        "factory.yaml",
        "log.txt",
    ]


def test_describer_that_fails_is_journaled_and_the_pr_opens_with_the_engines_text(factory: Harness) -> None:
    with_describer(factory, attempts=2)
    factory.executor.scripts["describe"] = AgentScript(fail="crash")

    result = factory.run(publish(factory))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    failed = factory.events(run_id, "DescriberFailed")
    assert [(event["attempt"], event["cause"]) for event in failed] == [(1, "executor_crash"), (2, "executor_crash")]
    assert "exited with code 1" in failed[0]["reason"]
    [pull] = factory.gh.pulls()
    assert pull == {
        "repo": "owner/target",
        "base": "main",
        "head": f"factory/{run_id}",
        "title": "Greeting",
        "body": f"Lands Spec #21 from Run {run_id}.\n\nCloses #28\nCloses #30\n",
    }


def test_output_off_the_schema_or_without_the_sections_is_a_failure_and_the_next_attempt_may_pass(
    factory: Harness,
) -> None:
    with_describer(factory, attempts=3)
    factory.executor.scripts["describe"] = [
        AgentScript(output={"title": "No body"}),
        AgentScript(output={"title": "Short", "body": "## Summary\n\nDone.\n"}),
        DESCRIPTION,
    ]

    result = factory.run(publish(factory))

    run_id = run_id_of(result.out)
    failed = factory.events(run_id, "DescriberFailed")
    assert [(event["attempt"], event["cause"]) for event in failed] == [(1, "invalid_output"), (2, "invalid_output")]
    assert "'body' is a required property" in failed[0]["reason"]
    assert failed[1]["reason"] == "the body lacks Evidence, Merge Danger, Door, Blast Radius"
    assert factory.gh.pulls()[0]["title"] == "Append a line to the log"


def test_describer_that_hangs_is_killed_at_its_timeout_and_the_pr_opens_with_the_engines_text(factory: Harness) -> None:
    with_describer(factory, attempts=1, timeout="1m")
    factory.executor.scripts["describe"] = AgentScript(fail="hang")

    result = factory.run(publish(factory))

    run_id = run_id_of(result.out)
    [failed] = factory.events(run_id, "DescriberFailed")
    assert (failed["cause"], failed["reason"]) == ("timeout", "no result after 60s")
    assert factory.gh.pulls()[0]["title"] == "Greeting"


def test_pipeline_without_a_describer_keeps_the_engines_text(factory: Harness) -> None:
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(publish(factory))

    run_id = run_id_of(result.out)
    assert factory.gh.pulls()[0]["body"] == f"Lands Spec #21 from Run {run_id}.\n\nCloses #28\nCloses #30\n"
    assert factory.events(run_id, "DescriberStarted") == []


def test_local_run_writes_the_pr_text_artifact_without_opening_a_pr(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": "Write hello.txt.\n"})
    with_describer(factory)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    body = (factory.run_dir(run_id) / "pull_request.md").read_text()
    assert body == f"{BODY}\nMerged:\n\n- t1 t1\n"
    assert not (factory.gh.root / "calls.jsonl").exists()
    assert factory.remote_branches() == {}


def test_local_run_without_a_describer_writes_the_engines_text_as_the_artifact(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": "Write hello.txt.\n"})
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    body = (factory.run_dir(run_id) / "pull_request.md").read_text()
    assert body == f"Lands Spec Greeting from Run {run_id}.\n\nMerged:\n\n- t1 t1\n"


def test_check_sees_the_pr_skill_at_the_describer_role(factory: Harness) -> None:
    with_describer(factory)

    assert factory.check().code == 0


def test_check_refuses_a_skill_the_agent_kit_lacks(factory: Harness) -> None:
    with_describer(factory)
    factory.pipeline["roles"]["describer"]["skills"] = ["pr", "tango"]

    result = factory.check()

    assert result.code == 2
    assert result.err.splitlines()[-1].endswith("roles.describer.skills: skill tango is not in the agent kit")


def test_check_refuses_a_describer_that_names_no_known_role(factory: Harness) -> None:
    factory.pipeline["describer"] = {"role": "writer", "attempts": 0, "tone": "dry"}

    result = factory.check()

    assert result.code == 2
    assert "describer: unknown Role writer" in result.err
    assert "describer.attempts" in result.err
    assert "describer.tone: unknown field" in result.err
