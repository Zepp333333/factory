"""What a Run learns while it lands and waits for CI is state its events tell, so a resumed Run knows it too."""

from pathlib import Path
from typing import Any

import pytest

from factory.core.events import Event
from factory.journal.commands import RUN_LEVEL
from factory.journal.run_state import RunMemory, apply
from factory.pipeline.pipeline import Pipeline

AT = "2026-10-30T12:00:00+00:00"


def told(*events: tuple[Event, dict[str, Any]]) -> RunMemory:
    pipeline = Pipeline(Path("pipeline"), "implement", {}, None, sandbox_retries=1)
    memory = RunMemory([], 1)
    for type, payload in events:
        apply(pipeline, memory, type, payload)
    return memory


def decided(node: str, outcome: str) -> tuple[Event, dict[str, Any]]:
    return Event.DecisionDecided, {"ticket": RUN_LEVEL, "node": node, "round": 1, "outcome": outcome}


def test_landing_tells_the_pushed_commit_and_the_pr() -> None:
    memory = told(
        (Event.IntegrationBranchPushed, {"remote": "origin", "branch": "factory/r", "commit": "abc"}),
        (Event.PullRequestOpened, {"url": "https://example.com/pull/1", "head": "factory/r", "base": "main"}),
    )

    assert (memory.pushed, memory.pull_request) == ("abc", "https://example.com/pull/1")


def test_a_pr_recorded_in_a_folder_is_the_runs_pr() -> None:
    memory = told(
        (Event.PullRequestRecorded, {"path": "/specs/pull_requests/r.md", "head": "factory/r", "base": "main"})
    )

    assert memory.pull_request == "/specs/pull_requests/r.md"


@pytest.mark.parametrize(
    ("end", "ended"),
    [
        ((Event.CiFinished, {"outcome": "passed", "commit": "abc", "jobs": []}), "passed"),
        ((Event.CiFinished, {"outcome": "unchecked", "commit": "abc", "jobs": []}), "unchecked"),
        ((Event.CiAbsent, {"commit": "abc", "grace": 60}), "absent"),
    ],
)
def test_the_ci_wait_tells_how_it_ended(end: tuple[Event, dict[str, Any]], ended: str) -> None:
    memory = told((Event.CiStarted, {"commit": "abc", "at": AT}), end)

    assert (memory.ci, memory.ci_ended) == (None, ended)


@pytest.mark.parametrize("outcome", ["failed", "timeout"])
def test_red_ci_ends_only_when_the_owner_accepts_it(outcome: str) -> None:
    red = told(
        (Event.CiStarted, {"commit": "abc", "at": AT}),
        (Event.CiFinished, {"outcome": outcome, "commit": "abc", "jobs": []}),
    )
    assert red.ci_ended == ""

    accepted = told(
        (Event.CiStarted, {"commit": "abc", "at": AT}),
        (Event.CiFinished, {"outcome": outcome, "commit": "abc", "jobs": []}),
        decided("ci_failed", "accept"),
    )

    assert (accepted.ci, accepted.ci_ended) == (None, outcome)


def test_only_the_owners_abort_aborts_the_run() -> None:
    assert told(decided("infra_failure", "abort")).aborted
    assert not told(decided("infra_failure", "retry")).aborted


@pytest.mark.parametrize(
    ("absent", "unseen"),
    [
        ({"commit": "abc", "grace": 180.0, "unseen": True}, True),
        ({"commit": "abc", "grace": 180.0}, True),
        ({"commit": "abc"}, False),
    ],
    ids=["by_grace", "by_grace_before_unseen", "host_runs_no_ci"],
)
def test_a_ci_absent_by_its_grace_is_unseen_also_in_a_journal_from_before_unseen_and_a_new_wait_clears_it(
    absent: dict[str, Any], unseen: bool
) -> None:
    started = (Event.CiStarted, {"commit": "abc", "url": "pr", "at": AT})

    assert told(started, (Event.CiAbsent, absent)).ci_unseen is unseen
    assert told(started, (Event.CiAbsent, absent), started).ci_unseen is False
