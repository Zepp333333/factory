import contextlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

from factory.pipeline.library_lookup import Layer, Library
from factory.pipeline.pipeline import BodyRules
from harness import AgentScript, Harness, run_id_of

APPEND = AgentScript(run=["echo line >> log.txt"], output={"summary": "Append a line", "handoff": "appended"})

BODY = (
    "## Summary\n\n```diff\n+line\n```\n\n"
    "## Evidence\n\nBefore: no log.txt. After: log.txt holds a line.\n\n"
    "## Merge Danger\n\n- **Door**: two-way, a revert undoes it.\n- **Blast Radius**: single-file, log.txt.\n"
)
DESCRIPTION = AgentScript(output={"title": "Append a line to the log", "body": BODY})

# A body from a project's own PR template, which has none of the default headings.
TEMPLATE_BODY = "## What changed\n\nAppends a line to log.txt.\n\n## Risks (rated)\n\nLow.\n"


def with_describer(factory: Harness, **fields: object) -> None:
    factory.pipeline["describer"] = {"role": "describer", **fields}
    factory.executor.scripts["implement"] = APPEND
    factory.executor.scripts["describe"] = DESCRIPTION


def with_project_describer(factory: Harness, body: dict[str, Any] | None = None, **fields: object) -> None:
    """Points the Describer at a Role of its own, which requires the headings and words of `body`, or none."""
    role: dict[str, Any] = {"prompt": "prompts/describer.md", "output": "schemas/describer.json"}
    if body is not None:
        role["body"] = body
    factory.pipeline["roles"]["template-describer"] = role
    with_describer(factory, **fields)
    factory.pipeline["describer"]["role"] = "template-describer"


def publish(factory: Harness) -> str:
    factory.gh.issue(28, "Greet", "Write hello.txt.")
    factory.gh.issue(30, "Greet again", "Write bye.txt.", blocked_by=[28])
    return factory.gh.issue(21, "Greeting", "Say hello.", sub_issues=[28, 30])


def test_describer_reads_a_brief_of_the_merged_tickets_in_a_throwaway_workspace_and_its_text_opens_the_pr(
    factory: Harness,
) -> None:
    factory.gh.issue(28, "Greet", "Write hello.txt.")
    factory.gh.issue(30, "Greet again", "Write bye.txt.", blocked_by=[28])
    factory.gh.issue(31, "Wave", "Write wave.txt.")
    url = factory.gh.issue(21, "Greeting", "Say hello.", sub_issues=[28, 30, 31])
    with_describer(factory)
    factory.executor.ticket_scripts[("31", "implement")] = AgentScript(
        files={"wave.txt": "wave\n"}, output=APPEND.output
    )
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["gates"] = {
        "build": {
            "kind": "command",
            "with": {"run": "test ! -f wave.txt"},
            "next": {"pass": "catch_up", "fail": "abandoned"},
        }
    }
    factory.executor.scripts["describe"] = AgentScript(
        files={"junk.txt": "scribbles\n"},
        output={
            "title": "Append a line to the log",
            "body": BODY + "\nCloses #28, fixes: #99, resolved owner/repo#5, prefixes #7\n",
        },
    )
    before = factory.branches()

    result = factory.run(url)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    run_dir = factory.run_dir(run_id)
    [pull] = factory.gh.pulls()
    assert pull["title"] == "Append a line to the log"
    assert pull["body"] == (
        f"{BODY}\nRefs #28, Refs: #99, Refs owner/repo#5, prefixes #7\n\n"
        "Closes #28\nCloses #30\n\n"
        "Not landed:\n\n"
        f"- #31 Wave: abandoned, its work is in `refs/factory/{run_id}/abandoned/31`,"
        " a ref in the repository of whoever ran the Run, not pushed\n"
    )
    assert (run_dir / "pull_request.md").read_text() == pull["body"]
    assert [event["title"] for event in factory.events(run_id, "PullRequestDescribed")] == ["Append a line to the log"]
    assert factory.events(run_id, "DescriberFailed") == []

    [started] = factory.events(run_id, "DescriberStarted")
    brief = started["brief"]
    base = factory.git("rev-parse", "main")
    assert (brief["base"], brief["base_ref"], brief["integration_branch"]) == (base, "main", f"factory/{run_id}")
    sources = [(entry["source"], entry.get("ticket"), entry.get("node")) for entry in brief["inputs"]]
    assert sources == [
        ("spec", None, None),
        ("ticket", "28", None),
        ("stage", "28", "implement"),
        ("gate", "28", "build"),
        ("ticket", "30", None),
        ("stage", "30", "implement"),
        ("gate", "30", "build"),
    ]
    assert brief["inputs"][0]["path"] == str(run_dir / "spec.md")
    assert json.loads(Path(brief["inputs"][2]["path"]).read_text())["handoff"] == "appended"
    assert json.loads(Path(brief["inputs"][3]["path"]).read_text())["outcome"] == "pass"
    request = factory.executor.requests[-1]
    assert (request.stage, request.settings.model) == ("describe", "sonnet")

    assert not (factory.home / "workspaces" / f"describer-{run_id}").exists()
    assert set(factory.branches()) == set(before) | {f"factory/{run_id}"}
    assert "junk.txt" in (run_dir / "describer" / "attempt-1" / "discarded.patch").read_text()
    assert factory.git("ls-tree", "-r", "--name-only", f"factory/{run_id}").splitlines() == [
        "README.md",
        "factory.yaml",
        "log.txt",
    ]
    assert factory.events(run_id, "DescriberSandboxRemoved") == [{"sandbox": f"describer-{run_id}"}]
    assert factory.events(run_id, "DescriberCleanupFailed") == []


def test_describer_that_fails_every_attempt_is_journaled_with_each_cause_and_the_engines_text_stands_in(
    factory: Harness,
) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": "---\ntitle: Greet\n---\nWrite hello.txt.\n"})
    with_describer(factory, attempts=6, timeout="1m")
    factory.executor.scripts["describe"] = [
        AgentScript(fail="crash"),
        AgentScript(output={"title": "No body"}),
        AgentScript(output={"title": "Short", "body": "## Summary\n\nDone.\n"}),
        AgentScript(output={"title": "Two\nlines", "body": BODY}),
        AgentScript(output={"title": " padded ", "body": BODY}),
        AgentScript(fail="hang"),
    ]
    factory.sandbox.left_behind = "sbx rm failed, and the stopped Sandbox stays behind"

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    failed = factory.events(run_id, "DescriberFailed")
    assert [(event["attempt"], event["cause"]) for event in failed] == [
        (1, "executor_crash"),
        (2, "invalid_output"),
        (3, "invalid_output"),
        (4, "invalid_output"),
        (5, "invalid_output"),
        (6, "timeout"),
    ]
    reasons = [event["reason"] for event in failed]
    assert "exited with code 1" in reasons[0]
    assert "'body' is a required property" in reasons[1]
    assert reasons[2] == "the body lacks Evidence, Merge Danger, Door, Blast Radius"
    assert "one line" in reasons[3]
    assert "one line" in reasons[4]
    assert reasons[5] == "no result after 60s"
    assert factory.events(run_id, "PullRequestDescribed") == []
    run_dir = factory.run_dir(run_id)
    assert (
        run_dir / "pull_request.md"
    ).read_text() == f"Lands Spec Greeting from Run {run_id}.\n\nMerged:\n\n- t1 Greet\n"
    assert (run_dir / "pull_request_title.txt").read_text() == "Greeting: Greet\n"
    assert (
        factory.events(run_id, "DescriberSandboxLeftBehind")
        == [{"sandbox": f"describer-{run_id}", "reason": "sbx rm failed, and the stopped Sandbox stays behind"}] * 6
    )
    assert factory.events(run_id, "DescriberSandboxRemoved") == []
    assert factory.events(run_id, "DescriberCleanupFailed") == []


def test_local_run_writes_the_text_of_a_describer_whose_role_requires_no_headings_and_its_names_meet_no_ticket(
    factory: Harness,
) -> None:
    spec = factory.write_spec(
        "Greeting", "Say hello.", {"describe": "Write hello.txt.\n", "describer": "Write b.txt.\n"}
    )
    with_project_describer(factory)
    factory.executor.scripts["describe"] = AgentScript(output={"title": "[#21] Characterize", "body": TEMPLATE_BODY})
    factory.executor.ticket_scripts[("describe", "implement")] = APPEND
    factory.executor.ticket_scripts[("describer", "implement")] = AgentScript(
        files={"b.txt": "b\n"}, output=APPEND.output
    )
    factory.sandbox.refuse_remove = True

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "DescriberFailed") == []
    run_dir = factory.run_dir(run_id)
    assert (run_dir / "pull_request.md").read_text() == (
        f"{TEMPLATE_BODY}\nMerged:\n\n- describe describe\n- describer describer\n"
    )
    assert (run_dir / "pull_request_title.txt").read_text() == "[#21] Characterize\n"
    assert not (factory.gh.root / "calls.jsonl").exists()
    assert factory.remote_branches() == {}

    request = factory.executor.requests[-1]
    assert request.stage == "describe"
    assert request.sandbox == f"describer-{run_id}"
    assert request.workspace == factory.home / "workspaces" / f"describer-{run_id}" / "workspace"
    assert {r.sandbox for r in factory.executor.requests[:-1]} == {f"{run_id}-describe", f"{run_id}-describer"}

    [cleanup] = factory.events(run_id, "DescriberCleanupFailed")
    assert "did not stop" in cleanup["reason"]
    assert factory.events(run_id, "DescriberSandboxRemoved") == []


def test_describer_waits_for_a_slot_that_other_runs_hold_and_holds_one_while_it_works(factory: Harness) -> None:
    factory.machine_config["slots"] = 2
    with_describer(factory)
    db = factory.home / "factory.db"
    insert = "c.executemany('INSERT INTO slots VALUES (?, ?)', [('other', 'x'), ('other', 'y')])"
    take = f"import sqlite3; c = sqlite3.connect('{db}'); {insert}; c.commit()"
    factory.executor.ticket_scripts[("30", "implement")] = AgentScript(
        run=["echo line >> log.txt", f'{sys.executable} -c "{take}"'], output=APPEND.output
    )
    seen = factory.root / "slots-seen-by-describer"
    count = f"import sqlite3; print(*sqlite3.connect('{db}').execute('SELECT * FROM slots'))"
    factory.executor.scripts["describe"] = AgentScript(
        output=DESCRIPTION.output, run=[f'{sys.executable} -c "{count}" > {seen}']
    )
    waited: list[list[str]] = []

    def free_after_the_merges(seconds: float) -> None:
        with contextlib.closing(sqlite3.connect(db)) as journal, journal:
            merges = journal.execute(
                "SELECT COUNT(*) FROM events WHERE type = 'OperationFinished' AND payload LIKE '%merged%'"
            )
            if merges.fetchone()[0] == 2 and factory.slots() == [("other", "x"), ("other", "y")]:
                waited.append(
                    [type for (type,) in journal.execute("SELECT type FROM events WHERE type LIKE 'Describer%'")]
                )
                journal.execute("DELETE FROM slots WHERE run_id = 'other'")

    factory.clock.on_sleep = free_after_the_merges

    result = factory.run(publish(factory))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert waited == [[]]
    assert [e["attempt"] for e in factory.events(run_id, "DescriberStarted")] == [1]
    assert seen.read_text().split() == [f"('{run_id}',", "'/describer')"]
    assert factory.slots() == []


def test_run_without_a_merge_starts_no_describer(factory: Harness) -> None:
    with_describer(factory)
    factory.pipeline["defaults"]["sandbox_retries"] = 0
    factory.sandbox.refuse = 2

    result = factory.run(publish(factory))

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "DescriberStarted") == []
    assert factory.gh.pulls() == []
    assert [r.stage for r in factory.executor.requests if r.stage == "describe"] == []


def test_describer_role_checks_only_the_headings_and_words_it_requires_and_the_run_freezes_them(
    factory: Harness,
) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": "Write hello.txt.\n"})
    with_project_describer(factory, {"headings": ["What changed", "Risks (rated)"], "words": ["log.txt"]}, attempts=3)
    factory.executor.scripts["describe"] = [
        AgentScript(output={"title": "Short", "body": "## What changed\n\nA line.\n"}),
        AgentScript(output={"title": "Default", "body": BODY}),
        AgentScript(output={"title": "Template", "body": TEMPLATE_BODY}),
    ]

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    failed = factory.events(run_id, "DescriberFailed")
    assert [event["reason"] for event in failed] == [
        "the body lacks Risks (rated), log.txt",
        "the body lacks What changed, Risks (rated)",
    ]
    run_dir = factory.run_dir(run_id)
    assert (run_dir / "pull_request_title.txt").read_text() == "Template\n"
    assert [event["attempt"] for event in factory.events(run_id, "PullRequestDescribed")] == [3]
    frozen = Library([Layer("run", run_dir / "library")]).role("template-describer")
    assert frozen.body == BodyRules(headings=("What changed", "Risks (rated)"), words=("log.txt",))


def test_check_sees_the_pr_skill_at_the_describer_role(factory: Harness) -> None:
    with_describer(factory)

    assert factory.check().code == 0


def test_check_refuses_a_skill_the_agent_kit_lacks(factory: Harness) -> None:
    with_describer(factory)
    factory.pipeline["roles"]["describer"]["skills"] = ["pr", "tango"]

    result = factory.check()

    assert result.code == 2
    assert result.err.splitlines()[-1].endswith(
        "describer: role describer in factory names skill tango, which is in no Layer"
    )


def test_check_refuses_a_describer_that_names_no_known_role(factory: Harness) -> None:
    factory.pipeline["describer"] = {"role": "writer", "attempts": 0, "tone": "dry"}

    result = factory.check()

    assert result.code == 2
    assert "describer: role writer is in no Layer" in result.err
    assert "describer.attempts" in result.err
    assert "describer.tone: unknown field" in result.err


@pytest.mark.parametrize(
    "body",
    [
        "Summary",
        {"headings": "Summary"},
        {"headings": [""]},
        {"headings": ["Two\nlines"]},
        {"headings": [" Summary"]},
        {"words": [1]},
        {"titles": ["Summary"]},
    ],
)
def test_check_reports_a_describer_role_whose_body_rules_are_malformed(factory: Harness, body: object) -> None:
    with_project_describer(factory, {})
    factory.pipeline["roles"]["template-describer"]["body"] = body

    result = factory.check()

    assert result.code == 2
    assert (
        "role template-describer in factory: `body` must map `headings` and `words` to lists of one-line names"
        in result.err
    )


def test_check_reports_body_rules_on_a_role_that_is_no_describer(factory: Harness) -> None:
    factory.pipeline["roles"]["implementer"]["body"] = {"headings": ["Summary"]}

    result = factory.check()

    assert result.code == 2
    assert "stages.implement: Role implementer: only the Describer reads `body`" in result.err
