import json
import os
import re
from typing import Any

import pytest

from factory.config import display_path
from harness import EXIT_GRACE_SECONDS, AgentScript, Harness, group_ended, run_id_of, wait_until

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
DONE = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})


def attempt_record(factory: Harness, attempt: int, stage: str = "implement") -> dict[str, Any]:
    requests = factory.executor.requests
    [request] = [r for r in requests if r.stage == stage and r.attempt_dir.name == f"attempt-{attempt}"]
    record: dict[str, Any] = json.loads((request.attempt_dir / "attempt.json").read_text())
    return record


def test_failed_attempts_are_replaced_with_the_same_brief_and_their_changes_go_to_a_patch(factory: Harness) -> None:
    factory.pipeline["defaults"].update({"attempts": 3, "timeout": "5s"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    broken = AgentScript(files={"README.md": "# Broken\n", "junk/half.txt": "half\n"}, fail="crash")
    hung = AgentScript(files={"junk.txt": "half\n"}, fail="hang")
    factory.executor.scripts["implement"] = [broken, hung, DONE]

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    integration = f"factory/{run_id}"
    assert factory.git("ls-tree", "--name-only", integration).splitlines() == ["README.md", "factory.yaml", "hello.txt"]
    assert factory.git("show", f"{integration}:README.md") == "# Target"
    briefs = [json.loads(r.brief.read_text()) for r in factory.executor.requests]
    assert [b["attempt"] for b in briefs] == [1, 2, 3]
    same = [{k: v for k, v in b.items() if k not in ("attempt", "output_dir")} for b in briefs]
    assert same[0] == same[1] == same[2]

    crashed, timed_out, done = (attempt_record(factory, n) for n in (1, 2, 3))
    assert crashed["cause"] == "executor_crash"
    assert crashed["head_before"] == factory.git("rev-parse", "main")
    patch = factory.executor.requests[0].attempt_dir / "discarded.patch"
    assert crashed["discarded"] == str(patch)
    assert not os.access(patch, os.W_OK)
    assert "+# Broken" in patch.read_text()
    assert "+half" in patch.read_text()
    assert timed_out["cause"] == "timeout"
    assert "+half" in (factory.executor.requests[1].attempt_dir / "discarded.patch").read_text()
    assert done["outcome"] == "done"

    # The timeout killed the hung agent with the tool it started, which would write `late.txt` had it lived.
    assert wait_until(lambda: group_ended(factory.sandbox.started[1]), EXIT_GRACE_SECONDS)
    assert not (factory.workspace(run_id, "t1") / "late.txt").exists()


def test_stage_exits_with_failed_after_its_last_attempt_and_each_failure_carries_its_class(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["attempts"] = 2
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [
        AgentScript(fail="auth"),
        AgentScript(files={"junk.txt": "half\n"}, fail="crash"),
    ]

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    finished = factory.events(run_id, "AttemptFinished")
    assert [(e["outcome"], e["cause"]) for e in finished] == [("failed", "auth"), ("failed", "executor_crash")]
    assert factory.git("rev-parse", f"factory/{run_id}") == factory.git("rev-parse", "main")
    row = factory.cli("status", run_id).out.splitlines()[2]
    assert row.split()[:7] == ["t1", "abandoned", "implement", "1", "2", "0", "abandoned"]
    assert factory.git("for-each-ref", "refs/factory") == ""


def test_output_that_breaks_the_role_schema_fails_as_an_executor_crash_and_is_never_committed(
    factory: Harness,
) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n"}, output={"summary": "no handoff"}
    )

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    [finished] = factory.events(run_id, "AttemptFinished")
    assert finished["cause"] == "executor_crash"
    assert finished["errors"] == ["'handoff' is a required property"]
    assert factory.git("rev-parse", f"factory/{run_id}") == factory.git("rev-parse", "main")
    assert factory.git("for-each-ref", "refs/factory") == ""
    assert not factory.workspace(run_id, "t1").exists()
    assert factory.cli("status", run_id).out.splitlines() == [
        f"Run {run_id}  partial  factory/{run_id}  {factory.run_took(run_id)}  $0.00",
        "TICKET  NODE       LAST STAGE  ROUND  ATTEMPT  LOST  STATE      DURATION  COST",
        f"t1      abandoned  implement   1      1        0     abandoned  {factory.ticket_took(run_id, 't1'):8}  $0.00",
    ]


def test_a_silent_attempt_is_killed_as_stalled_and_failed_takes_its_own_route(factory: Harness) -> None:
    factory.pipeline["defaults"].update({"timeout": "1h", "stall": "1m"})
    factory.pipeline["stages"]["implement"]["next"]["failed"] = "rescue"
    factory.pipeline["stages"]["rescue"] = {"role": "implementer", "inputs": ["ticket"], "next": {"done": "merge"}}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"junk.txt": "half\n"}, heartbeat=True)
    factory.executor.scripts["rescue"] = DONE

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    stalled, rescued = factory.events(run_id, "AttemptFinished")
    # The agent's heartbeat keeps coming, but it makes no new message, so the stall kills it.
    assert (stalled["outcome"], stalled["cause"]) == ("failed", "stalled")
    assert 60 < stalled["silent"] < 3600
    assert "+half" in (factory.executor.requests[0].attempt_dir / "discarded.patch").read_text()
    timeline = factory.cli("status", run_id, "--timeline").out
    assert re.search(r"implement\s+failed\s+stalled  silent 1 min \d+ s", timeline)
    assert rescued["outcome"] == "done"
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    nodes = [e["node"] for e in factory.events(run_id, "NodeEntered")]
    assert nodes == ["implement", "rescue", "merge", "done"]


@pytest.mark.parametrize(
    ("where", "stall", "timeout", "seconds"), [("stages.implement", "10m", "10m", 600), ("defaults", "2h", "1h", 3600)]
)
def test_check_refuses_a_stall_that_is_not_less_than_timeout(
    factory: Harness, where: str, stall: str, timeout: str, seconds: int
) -> None:
    section, _, name = where.partition(".")
    block = factory.pipeline[section][name] if name else factory.pipeline[section]
    block.update({"timeout": timeout, "stall": stall})

    result = factory.check()

    assert result.code == 2
    assert result.err.splitlines() == [
        f"factory: {display_path(factory.pipeline_file)}: {where}.stall: must be less than timeout {seconds}s"
    ]


def test_read_only_stage_changes_go_to_a_patch_and_the_journal_records_the_violation(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = "review"
    factory.pipeline["stages"]["review"] = {
        "role": "implementer",
        "workspace": "ro",
        "inputs": ["ticket"],
        "next": {"done": "merge"},
    }
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = DONE
    factory.executor.scripts["review"] = AgentScript(
        files={"hello.txt": "tampered\n", "notes.txt": "x\n"}, output={"summary": "Looks fine", "handoff": "ok"}
    )

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    integration = f"factory/{run_id}"
    assert factory.git("show", f"{integration}:hello.txt") == "hello"
    assert factory.git("log", "-1", "--format=%s", integration) == "Add hello.txt"
    review = attempt_record(factory, 1, stage="review")
    assert review["outcome"] == "done"
    assert review["head_after"] == review["head_before"]
    [violation] = factory.events(run_id, "WorkspaceViolated")
    assert violation["node"] == "review"
    assert violation["files"] == ["hello.txt", "notes.txt"]
    assert "+tampered" in (factory.executor.requests[1].attempt_dir / "discarded.patch").read_text()


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("attempts", 0, "stages.implement.attempts: must be a positive integer"),
        ("attempts", "two", "stages.implement.attempts: must be a positive integer"),
        ("timeout", "soon", "stages.implement.timeout: must be a duration such as 90s, 45m or 2h"),
        ("timeout", 0, "stages.implement.timeout: must be a duration such as 90s, 45m or 2h"),
    ],
)
def test_check_refuses_bad_attempts_and_timeout(factory: Harness, field: str, value: object, error: str) -> None:
    factory.pipeline["stages"]["implement"][field] = value

    result = factory.check()

    assert result.code == 2
    assert result.err.splitlines() == [f"factory: {display_path(factory.pipeline_file)}: {error}"]


def test_check_reports_a_bad_default_once_where_it_is_set(factory: Harness) -> None:
    factory.pipeline["defaults"]["timeout"] = "soon"
    factory.pipeline["stages"]["review"] = {"role": "implementer", "inputs": ["ticket"], "next": {"done": "merge"}}

    result = factory.check()

    assert result.err.splitlines() == [
        f"factory: {display_path(factory.pipeline_file)}: defaults.timeout: must be a duration such as 90s, 45m or 2h"
    ]


def test_attempts_the_sandbox_lost_or_a_rate_limit_ended_spend_no_attempt_and_a_rate_limit_spends_no_loss(
    factory: Harness,
) -> None:
    factory.sandbox.refuse = 2
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [DONE, DONE, AgentScript(fail="rate_limited"), DONE]

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    finished = factory.events(run_id, "AttemptFinished")
    assert [e.get("cause") for e in finished] == ["sandbox_start", "sandbox_start", "rate_limited", None]
    assert "did not start" in finished[0]["reason"]
    # Two losses are all that `sandbox_retries` of 2 allows, so a rate limit counted as a loss would fail the Stage.
    assert factory.events(run_id, "SandboxRetriesExhausted") == []
    assert [e["attempt"] for e in factory.events(run_id, "AttemptStarted")] == [1, 2, 3, 4]
    assert [json.loads(r.brief.read_text())["round"] for r in factory.executor.requests] == [1, 1, 1, 1]
    assert [e["round"] for e in factory.events(run_id, "NodeEntered") if "round" in e] == [1]
    row = factory.cli("status", run_id).out.splitlines()[2]
    assert row.split()[:7] == ["t1", "done", "implement", "1", "4", "2", "merged"]


def test_stage_fails_with_the_last_loss_once_sandbox_retries_are_used_up_and_names_it(factory: Harness) -> None:
    factory.pipeline["defaults"]["sandbox_retries"] = 1
    factory.pipeline["defaults"]["attempts"] = 5
    factory.sandbox.refuse = 5
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = DONE

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    finished = factory.events(run_id, "AttemptFinished")
    assert [(e["outcome"], e["cause"]) for e in finished] == [("failed", "sandbox_start")] * 2
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")] == ["implement", "abandoned"]
    [exhausted] = factory.events(run_id, "SandboxRetriesExhausted")
    assert (exhausted["ticket"], exhausted["node"], exhausted["losses"]) == ("t1", "implement", 2)
    assert exhausted["cause"] == "sandbox_start"
    assert exhausted["reason"] == finished[-1]["reason"]
    assert "did not start" in exhausted["reason"]
    lines = factory.cli("status", run_id).out.splitlines()
    assert f"Ticket t1 lost its Sandbox 2x, last: sandbox_start in implement: {exhausted['reason']}" in lines
