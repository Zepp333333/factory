import json
import os
import re
import time
from typing import Any

import pytest

from factory.config import display_path
from harness import AgentScript, Harness, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
DONE = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})


def attempt_record(factory: Harness, attempt: int, stage: str = "implement") -> dict[str, Any]:
    requests = factory.executor.requests
    [request] = [r for r in requests if r.stage == stage and r.attempt_dir.name == f"attempt-{attempt}"]
    record: dict[str, Any] = json.loads((request.attempt_dir / "attempt.json").read_text())
    return record


def test_failed_attempt_is_replaced_by_a_new_one_with_the_same_brief(factory: Harness) -> None:
    factory.pipeline["defaults"]["attempts"] = 2
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [AgentScript(files={"junk.txt": "half\n"}, fail="crash"), DONE]

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    integration = f"factory/{run_id}"
    assert factory.git("ls-tree", "--name-only", integration).splitlines() == ["README.md", "factory.yaml", "hello.txt"]
    first, second = (json.loads(r.brief.read_text()) for r in factory.executor.requests)
    assert {k: v for k, v in first.items() if k not in ("attempt", "output_dir")} == {
        k: v for k, v in second.items() if k not in ("attempt", "output_dir")
    }
    assert (first["attempt"], second["attempt"]) == (1, 2)
    assert attempt_record(factory, 1)["cause"] == "executor_crash"
    assert attempt_record(factory, 2)["outcome"] == "done"


def test_stage_exits_with_failed_after_its_last_attempt(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["attempts"] = 2
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"junk.txt": "half\n"}, fail="crash")

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    assert len(factory.executor.requests) == 2
    assert factory.git("rev-parse", f"factory/{run_id}") == factory.git("rev-parse", "main")
    row = factory.cli("status", run_id).out.splitlines()[2]
    assert row.split() == ["t1", "abandoned", "implement", "1", "2", "0", "abandoned"]
    assert factory.git("for-each-ref", "refs/factory") == ""


def test_failed_is_an_outcome_of_its_own_with_its_own_route(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"]["failed"] = "rescue"
    factory.pipeline["stages"]["rescue"] = {"role": "implementer", "inputs": ["ticket"], "next": {"done": "merge"}}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(fail="crash")
    factory.executor.scripts["rescue"] = DONE

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    nodes = [e["node"] for e in factory.events(run_id, "NodeEntered")]
    assert nodes == ["implement", "rescue", "merge", "done"]


def test_uncommitted_changes_of_a_failed_attempt_go_to_a_patch_and_the_workspace_is_reset(factory: Harness) -> None:
    factory.pipeline["defaults"]["attempts"] = 2
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    broken = AgentScript(files={"README.md": "# Broken\n", "junk/half.txt": "half\n"}, fail="crash")
    factory.executor.scripts["implement"] = [broken, DONE]

    run_id = run_id_of(factory.run(spec).out)

    first = attempt_record(factory, 1)
    patch = factory.executor.requests[0].attempt_dir / "discarded.patch"
    assert first["discarded"] == str(patch)
    assert not os.access(patch, os.W_OK)
    text = patch.read_text()
    assert "+# Broken" in text
    assert "+half" in text
    assert first["head_before"] == factory.git("rev-parse", "main")
    assert factory.git("show", f"factory/{run_id}:README.md") == "# Target"


def test_a_hung_attempt_times_out(factory: Harness) -> None:
    factory.pipeline["defaults"].update({"attempts": 2, "timeout": "5s"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [AgentScript(files={"junk.txt": "half\n"}, fail="hang"), DONE]

    result = factory.run(spec)

    assert result.code == 0, result.out
    first = attempt_record(factory, 1)
    assert first["cause"] == "timeout"
    assert "+half" in (factory.executor.requests[0].attempt_dir / "discarded.patch").read_text()
    run_id = run_id_of(result.out)
    assert "junk.txt" not in factory.git("ls-tree", "--name-only", f"factory/{run_id}")
    time.sleep(0.5)
    assert not (factory.workspace(run_id, "t1") / "late.txt").exists()


def test_a_silent_attempt_is_killed_as_stalled_though_its_heartbeat_keeps_coming(factory: Harness) -> None:
    factory.pipeline["defaults"].update({"attempts": 2, "timeout": "1h", "stall": "5m"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [AgentScript(files={"junk.txt": "half\n"}, heartbeat=True), DONE]

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    first, second = factory.events(run_id, "AttemptFinished")
    assert (first["outcome"], first["cause"]) == ("failed", "stalled")
    assert 300 < first["silent"] < 3600
    assert second["outcome"] == "done"
    assert "+half" in (factory.executor.requests[0].attempt_dir / "discarded.patch").read_text()


def test_status_shows_how_long_a_stalled_attempt_was_silent(factory: Harness) -> None:
    factory.pipeline["defaults"].update({"timeout": "1h", "stall": "5m"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(heartbeat=True)

    run_id = run_id_of(factory.run(spec).out)

    timeline = factory.cli("status", run_id, "--timeline").out
    assert re.search(r"implement\s+failed\s+stalled  silent 5 min \d+ s", timeline)


@pytest.mark.parametrize(("where", "stall", "timeout"), [("stages.implement", "10m", "10m"), ("defaults", "2h", "1h")])
def test_check_refuses_a_stall_that_is_not_less_than_timeout(
    factory: Harness, where: str, stall: str, timeout: str
) -> None:
    section, _, name = where.partition(".")
    block = factory.pipeline[section][name] if name else factory.pipeline[section]
    block.update({"timeout": timeout, "stall": stall})

    result = factory.check()

    assert result.code == 2
    seconds = 600 if timeout == "10m" else 3600
    assert result.err.splitlines() == [
        f"factory: {display_path(factory.pipeline_file)}: {where}.stall: must be less than timeout {seconds}s"
    ]


def test_auth_failure_carries_its_class(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(fail="auth")

    run_id = run_id_of(factory.run(spec).out)

    [finished] = factory.events(run_id, "AttemptFinished")
    assert (finished["outcome"], finished["cause"]) == ("failed", "auth")


def test_sandbox_that_does_not_start_fails_the_attempt(factory: Harness) -> None:
    factory.pipeline["defaults"]["attempts"] = 2
    factory.sandbox.refuse = 1
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = DONE

    result = factory.run(spec)

    assert result.code == 0, result.out
    first, second = factory.events(run_id_of(result.out), "AttemptFinished")
    assert (first["outcome"], first["cause"]) == ("failed", "sandbox_start")
    assert "did not start" in first["reason"]
    assert second["outcome"] == "done"


def test_attempts_the_sandbox_lost_spend_no_attempt_of_the_stage(factory: Harness) -> None:
    factory.sandbox.refuse = 2
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = DONE

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    causes = [e.get("cause") for e in factory.events(run_id, "AttemptFinished")]
    assert causes == ["sandbox_start", "sandbox_start", None]
    assert [json.loads(r.brief.read_text())["round"] for r in factory.executor.requests] == [1, 1, 1]
    assert [e["round"] for e in factory.events(run_id, "NodeEntered") if "round" in e] == [1]
    assert factory.cli("status", run_id).out.splitlines()[2].split() == [
        "t1",
        "done",
        "implement",
        "1",
        "3",
        "2",
        "merged",
    ]


def test_stage_fails_with_the_last_loss_once_sandbox_retries_are_used_up(factory: Harness) -> None:
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
    assert "did not start" in finished[-1]["reason"]
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")] == ["implement", "abandoned"]


def test_sandbox_retries_of_zero_fail_the_stage_on_the_first_loss(factory: Harness) -> None:
    factory.pipeline["defaults"]["sandbox_retries"] = 0
    factory.sandbox.refuse = 1
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = DONE

    result = factory.run(spec)

    assert result.code == 1
    assert len(factory.events(run_id_of(result.out), "AttemptFinished")) == 1


def test_output_that_breaks_the_role_schema_fails_as_an_executor_crash(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "x"})

    run_id = run_id_of(factory.run(spec).out)

    [finished] = factory.events(run_id, "AttemptFinished")
    assert finished["cause"] == "executor_crash"
    assert finished["errors"] == ["'handoff' is a required property"]
    assert factory.git("for-each-ref", "refs/factory") == ""
    assert not factory.workspace(run_id, "t1").exists()


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


def test_stage_that_ran_out_of_sandbox_retries_names_the_last_loss_in_the_journal_and_in_status(
    factory: Harness,
) -> None:
    factory.pipeline["defaults"]["sandbox_retries"] = 1
    factory.pipeline["defaults"]["attempts"] = 5
    factory.sandbox.refuse = 5
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = DONE

    run_id = run_id_of(factory.run(spec).out)

    [exhausted] = factory.events(run_id, "SandboxRetriesExhausted")
    assert (exhausted["ticket"], exhausted["node"], exhausted["losses"]) == ("t1", "implement", 2)
    assert (exhausted["cause"], "did not start" in exhausted["reason"]) == ("sandbox_start", True)
    lines = factory.cli("status", run_id).out.splitlines()
    assert f"Ticket t1 lost its Sandbox 2x, last: sandbox_start in implement: {exhausted['reason']}" in lines


def test_rate_limit_between_two_sandbox_losses_spends_no_loss(factory: Harness) -> None:
    factory.pipeline["defaults"]["sandbox_retries"] = 1
    factory.sandbox.refuse = 1
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [DONE, AgentScript(fail="rate_limited"), DONE]

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    causes = [e.get("cause") for e in factory.events(run_id, "AttemptFinished")]
    assert causes == ["sandbox_start", "rate_limited", None]
    assert factory.events(run_id, "SandboxRetriesExhausted") == []
    assert factory.cli("status", run_id).out.splitlines()[2].split()[5] == "1"


def test_attempt_numbers_do_not_repeat_after_a_loss(factory: Harness) -> None:
    factory.sandbox.refuse = 1
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = DONE

    run_id = run_id_of(factory.run(spec).out)

    assert [e["attempt"] for e in factory.events(run_id, "AttemptStarted")] == [1, 2]
