"""A Run that the factory journaled before the Describer and the CI fixer became two uses of one RunAgent still reads.

The Journal in `fixtures/journals/run-agents.jsonl` is such a Run, with a Describer and a CI fixer that pushed a fix,
and the outputs next to it are what `factory status` and `factory stats` printed for it then.
"""

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from factory.journal.journal import Journal
from harness import FakeClock, Harness

JOURNALS = Path(__file__).parent / "fixtures" / "journals"
RECORDED = JOURNALS / "run-agents.jsonl"


def replay(factory: Harness, until: Callable[[dict[str, Any]], bool] = lambda row: False) -> str:
    """Journals the recorded events at the times they were recorded, up to the first that `until` holds for, and
    returns the Run's id.

    The harness's clock then stands at the last of them, so the Run's heartbeat is fresh.
    """
    rows = [json.loads(line) for line in RECORDED.read_text().splitlines()]
    clock = FakeClock()
    journal = Journal(factory.home / "factory.db", clock)
    for row in rows:
        clock._now = datetime.fromisoformat(row["at"])
        journal.append(row["run_id"], row["type"], row["payload"])
        if until(row):
            break
    journal.close()
    factory.clock._now = clock.now()
    return str(rows[0]["run_id"])


def expected(name: str, run_id: str) -> str:
    return (JOURNALS / name).read_text().replace("<run>", run_id)


@pytest.mark.parametrize(
    ("argv", "golden"),
    [
        (("status", "<run>"), "run-agents.status.txt"),
        (("status", "<run>", "--timeline"), "run-agents.timeline.txt"),
        (("stats", "<run>"), "run-agents.stats.txt"),
    ],
    ids=["status", "timeline", "stats"],
)
def test_status_and_stats_read_a_run_journaled_before_the_run_agent(
    factory: Harness, argv: tuple[str, ...], golden: str
) -> None:
    run_id = replay(factory)

    result = factory.cli(*(run_id if arg == "<run>" else arg for arg in argv))

    assert result.code == 0, result.out + result.err
    assert result.out == expected(golden, run_id)


def describing(row: dict[str, Any]) -> bool:
    return bool(row["type"] == "DescriberStarted")


def fixing(row: dict[str, Any]) -> bool:
    return bool(row["type"] == "AttemptStarted" and row["payload"]["ticket"] == "/ci_fix")


@pytest.mark.parametrize(
    ("until", "golden"),
    [(describing, "run-agents.describing.txt"), (fixing, "run-agents.fixing.txt")],
    ids=["describing", "fixing"],
)
def test_status_shows_the_run_agent_at_work_in_a_run_journaled_before_it(
    factory: Harness, until: Callable[[dict[str, Any]], bool], golden: str
) -> None:
    run_id = replay(factory, until)

    result = factory.cli("status", run_id)

    assert result.code == 0, result.out + result.err
    assert result.out == expected(golden, run_id)
