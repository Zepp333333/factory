from pathlib import Path

import pytest

from factory.custody import HEARTBEAT_STALE_SECONDS
from harness import AgentScript, Harness, ticket_text
from test_github_run import APPEND, publish
from test_recovery import (
    HELLO,
    STALE,
    attempt_started,
    crashed_run,
    killed_runs_run_no_handler,
    status_line,
)

pytestmark = pytest.mark.usefixtures(killed_runs_run_no_handler.__name__)


def pair_crashed_mid_second(factory: Harness) -> tuple[Path, str]:
    """Crashes a Run whose t1 merged and whose t2 hangs in its first Attempt with half-written work."""
    spec = factory.write_spec("Pair", "Two.", {"t1": ticket_text("t1"), "t2": ticket_text("t2", ["t1"])})
    factory.executor.scripts["implement"] = [AgentScript(files={"t1.txt": "t1\n"}, output=HELLO.output)]
    factory.executor.ticket_scripts[("t2", "implement")] = [
        AgentScript(files={"half.txt": "half\n"}, output=HELLO.output, wait_for=factory.root / "never")
    ]
    run_id = crashed_run(factory, spec, lambda run_id: len(factory.events(run_id, "AttemptStarted")) == 2)
    return spec, run_id


def ticket_states(factory: Harness, run_id: str) -> dict[str, str]:
    lines = factory.cli("status", run_id).out.splitlines()[1:]
    return {cells[0]: cells[6] for cells in (line.split() for line in lines) if cells[0] in ("t1", "t2", "t3")}


def test_finish_abandons_unmerged_tickets_keeps_their_work_ends_the_run_partial_and_records_the_pr(
    factory: Harness,
) -> None:
    spec, run_id = pair_crashed_mid_second(factory)
    agent = factory.sandbox.started[-1]
    assert agent.poll() is None
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("finish", run_id)

    assert result.code == 0, result.out + result.err
    assert f"Run {run_id} finished: partial" in result.out
    assert status_line(factory, run_id).startswith(f"Run {run_id}  partial")
    assert ticket_states(factory, run_id) == {"t1": "merged", "t2": "abandoned"}
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["partial"]
    assert factory.slots() == []
    assert len(factory.events(run_id, "AttemptStarted")) == 2

    ref = f"refs/factory/{run_id}/abandoned/t2"
    assert factory.git("show", f"{ref}:half.txt") == "half"
    assert factory.git("show", f"{ref}:t1.txt") == "t1"
    assert f"factory/{run_id}--t2" not in factory.branches()
    assert not factory.workspace(run_id, "t2").exists()
    assert [e["ticket"] for e in factory.events(run_id, "SandboxRemoved")] == ["t1", "t2"]
    assert agent.poll() is not None
    assert f"Ticket t2 abandoned, its work is in {ref}" in factory.cli("status", run_id).out

    path = spec / "pull_requests" / f"factory--{run_id}.md"
    assert f"Pull request: {path}" in result.out
    assert "- t2 t2: abandoned, its work is in" in path.read_text()


def test_finish_abandons_tickets_that_never_started_and_opens_no_pr_without_a_merged_ticket(factory: Harness) -> None:
    spec = factory.write_spec("Pair", "Two.", {"t1": ticket_text("t1"), "t2": ticket_text("t2", ["t1"])})
    factory.executor.scripts["implement"] = AgentScript(wait_for=factory.root / "never")
    run_id = crashed_run(factory, spec, attempt_started(factory))
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("finish", run_id)

    assert result.code == 0, result.out + result.err
    assert f"Run {run_id} finished: partial" in result.out
    assert ticket_states(factory, run_id) == {"t1": "abandoned", "t2": "abandoned"}
    assert len(factory.events(run_id, "AttemptStarted")) == 1
    assert f"No Ticket of Run {run_id} merged, so no pull request was opened" in result.out
    assert "Pull request:" not in result.out
    assert factory.events(run_id, "PullRequestRecorded") == []
    assert not (spec / "pull_requests").exists()


def github_run_crashed(factory: Harness) -> str:
    """Crashes a Run of Spec #21 whose #28 merged and whose #30 hangs in its first Attempt."""
    url = publish(factory)
    factory.executor.scripts["implement"] = [APPEND]
    factory.executor.ticket_scripts[("30", "implement")] = [AgentScript(wait_for=factory.root / "never")]
    return crashed_run(factory, url, lambda run_id: len(factory.events(run_id, "AttemptStarted")) == 2)


def test_finish_pushes_and_opens_a_pr_that_closes_merged_tickets_and_lists_abandoned(factory: Harness) -> None:
    run_id = github_run_crashed(factory)
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("finish", run_id)

    assert result.code == 0, result.out + result.err
    assert f"factory/{run_id}" in factory.remote_branches()
    [pull] = factory.gh.pulls()
    assert pull["body"] == (
        f"Lands Spec #21 from Run {run_id}.\n\nCloses #28\n\nNot landed:\n\n- #30 Greet again: abandoned\n"
    )
    assert "Pull request:" in result.out


def test_finish_of_a_running_run_changes_nothing_and_says_why(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("t1")})
    factory.executor.scripts["implement"] = AgentScript(wait_for=factory.root / "never")
    run_id = crashed_run(factory, spec, attempt_started(factory))
    factory.clock.sleep(HEARTBEAT_STALE_SECONDS - 5)
    before = factory.all_events(run_id)

    result = factory.cli("finish", run_id)

    assert result.code == 2
    assert f"Run {run_id} is running, and only an orphaned Run finishes" in result.err
    assert factory.all_events(run_id) == before
    assert f"factory/{run_id}--t1" in factory.branches()
