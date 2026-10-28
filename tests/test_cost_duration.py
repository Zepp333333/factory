"""Cost and duration of every node: what the Journal stores and what the Run's output shows."""

import contextlib
import io
import re
import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from factory.cli import main
from harness import AgentScript, Harness, run_id_of, ticket_text
from test_deciders import RISK, deciding, review

TICKET = ticket_text("Greet")
DONE = AgentScript(
    files={"hello.txt": "hello\n"},
    output={"summary": "Add hello.txt", "handoff": "done"},
    turns=7,
    cost=0.42,
    tokens=(10, 20, 30, 40),
)
# The scripted agent adds a subagent of 1, 2, 3 and 4 tokens to every `tokens` it is given.
DONE_TOKENS = {"input_tokens": 11, "output_tokens": 22, "cache_read_tokens": 33, "cache_write_tokens": 44}


def jump_when_asked(factory: Harness, gate: Path, seconds: float) -> None:
    """Moves the injected clock `seconds` ahead on the Run's first sleep and then lets the waiting agent finish."""
    clock = factory.clock
    jumped: list[bool] = []

    def jump(_: float) -> None:
        if not jumped:
            jumped.append(True)
            clock._now += timedelta(seconds=seconds)
            gate.touch()

    clock.on_sleep = jump


def query(factory: Harness, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with contextlib.closing(sqlite3.connect(factory.home / "factory.db")) as db:
        return db.execute(sql, params).fetchall()


def clock_text(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}m {whole % 60:02d}s" if whole >= 60 else f"{whole}s"


def test_attempt_finished_writes_usage_from_model_usage_and_the_duration(factory: Harness) -> None:
    gate = factory.root / "go"
    factory.executor.scripts["implement"] = replace(DONE, wait_for=gate)
    jump_when_asked(factory, gate, 90)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [finished] = factory.events(run_id, "AttemptFinished")
    assert finished["usage"] == {"cost_usd": 0.42, "turns": 7, **DONE_TOKENS}
    assert 90 <= finished["duration"] < 91
    [started_at] = factory.times(run_id, "AttemptStarted")
    [finished_at] = factory.times(run_id, "AttemptFinished")
    assert finished["duration"] == (finished_at - started_at).total_seconds()


def test_failed_attempt_with_a_result_writes_its_usage_too(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["attempts"] = 2
    ran_out = AgentScript(fail="max_turns", turns=3, cost=0.3, tokens=(1, 1, 1, 1))
    factory.executor.scripts["implement"] = [ran_out, DONE]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    failed, done = factory.events(run_id, "AttemptFinished")
    assert failed["outcome"] == "failed"
    assert failed["usage"] == {
        "cost_usd": 0.3,
        "turns": 3,
        "input_tokens": 2,
        "output_tokens": 3,
        "cache_read_tokens": 4,
        "cache_write_tokens": 5,
    }
    assert "duration" in failed
    assert done["usage"]["cost_usd"] == 0.42


def test_attempt_without_a_result_writes_its_usage_as_unknown_not_zero(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["attempts"] = 2
    factory.executor.scripts["implement"] = [AgentScript(fail="crash"), DONE]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    crashed, _ = factory.events(run_id, "AttemptFinished")
    assert crashed["cause"] == "executor_crash"
    assert crashed["usage"] is None
    assert "duration" in crashed
    costs = query(factory, "SELECT attempt, cost_usd FROM attempts WHERE run_id = ? ORDER BY attempt", run_id)
    assert costs == [(1, None), (2, 0.42)]


def test_sums_show_that_they_hold_attempts_of_unknown_cost(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["attempts"] = 2
    factory.executor.scripts["implement"] = [AgentScript(fail="crash"), DONE]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    assert f"Run {run_id} finished: done in 0s, $0.42+?" in result.out.splitlines()
    status = factory.cli("status", run_id).out.splitlines()
    assert status[0].endswith("$0.42+?")
    assert status[2].split()[-1] == "$0.42+?"


def test_every_node_of_a_ticket_writes_its_duration_and_only_stages_have_a_cost(factory: Harness) -> None:
    deciding(factory, RISK)
    factory.pipeline["gates"] = {
        "check": {"kind": "command", "with": {"run": "true"}, "next": {"pass": "review", "fail": "abandoned"}}
    }
    factory.pipeline["stages"]["implement"]["next"]["done"] = "check"
    factory.executor.scripts["implement"] = DONE
    factory.executor.scripts["review"] = review("two_way", "module")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    events = {
        type: factory.events(run_id, type)
        for type in ("AttemptFinished", "GateFinished", "DecisionDecided", "OperationFinished")
    }
    assert {e["node"] for e in events["AttemptFinished"]} == {"implement", "review"}
    assert all(isinstance(e["usage"], dict) for e in events["AttemptFinished"])
    for type in ("AttemptFinished", "GateFinished", "DecisionDecided", "OperationFinished"):
        assert events[type], type
        assert all(isinstance(e["duration"], float) for e in events[type]), type
    for type in ("GateFinished", "DecisionDecided", "OperationFinished"):
        assert all("usage" not in e for e in events[type]), type


def test_attempts_projection_gives_the_price_of_a_stage_across_runs(factory: Harness) -> None:
    factory.executor.scripts["implement"] = DONE
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    for _ in range(2):
        factory.clock.sleep(1)
        assert factory.run(spec).code == 0

    rows = query(
        factory,
        "SELECT node, count(*), round(avg(cost_usd), 2), round(sum(cost_usd), 2) FROM attempts GROUP BY node",
    )

    assert rows == [("implement", 2, 0.42, 0.84)]
    columns = query(
        factory,
        "SELECT turns, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
        " started_at IS NOT NULL, finished_at IS NOT NULL, duration IS NOT NULL FROM attempts LIMIT 1",
    )
    assert columns == [(7, 11, 22, 33, 44, 1, 1, 1)]


def test_the_tree_shows_the_time_since_the_start_and_the_cost_so_far(factory: Harness) -> None:
    gate = factory.root / "go"
    factory.pipeline["stages"]["implement"]["next"]["done"] = "second"
    factory.pipeline["roles"]["again"] = factory.pipeline["roles"]["implementer"]
    factory.pipeline["stages"]["second"] = {"role": "again", "inputs": ["ticket"], "next": {"done": "sync"}}
    factory.executor.scripts["implement"] = DONE
    factory.executor.scripts["second"] = AgentScript(
        output={"handoff": "done"}, cost=0.1, wait_for=gate, files={"two.txt": "2\n"}
    )
    jump_when_asked(factory, gate, 125)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    lines = result.out.splitlines()
    # While `second` runs, its Attempt is not in the sum yet.
    assert any(re.fullmatch(r".*second round 1, attempt 1 +\d+s +\$0\.42", line) for line in lines)
    took = (factory.times(run_id, "RunFinished")[0] - factory.times(run_id, "RunStarted")[0]).total_seconds()
    assert clock_text(took).startswith("2m")
    assert f"Run {run_id}  [done]  factory/{run_id}  {clock_text(took)}  $0.52" in lines
    assert f"Run {run_id} finished: done in {clock_text(took)}, $0.52" in lines
    ticket = next(line for line in lines if line.startswith("  + t1"))
    assert re.fullmatch(r"  \+ t1 +done +merged, second round 1, attempt 1 +2m \d\ds +\$0\.52", ticket)


def test_status_shows_duration_and_cost_per_ticket_and_per_run(factory: Harness) -> None:
    factory.executor.scripts["implement"] = DONE
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET, "t2": ticket_text("Greet again")})
    run_id = run_id_of(factory.run(spec).out)

    lines = factory.cli("status", run_id).out.splitlines()

    assert lines[0] == f"Run {run_id}  done  factory/{run_id}  0s  $0.84"
    assert lines[1].split()[-2:] == ["DURATION", "COST"]
    assert [row.split()[-2:] for row in lines[2:4]] == [["0s", "$0.42"], ["0s", "$0.42"]]


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def run_with_out(factory: Harness, out: io.StringIO, *argv: str) -> int:
    machine = factory.machine()
    with contextlib.redirect_stdout(out):
        return main(list(argv), machine)


def waiting_run(factory: Harness, out: io.StringIO) -> str:
    """Runs a Ticket whose agent waits while the clock moves 5 s on each of the first 3 sleeps."""
    gate = factory.root / "go"
    factory.executor.scripts["implement"] = replace(DONE, wait_for=gate)
    sleeps: list[float] = []

    def move(_: float) -> None:
        sleeps.append(1)
        factory.clock._now += timedelta(seconds=5)
        if len(sleeps) == 3:
            gate.touch()

    factory.clock.on_sleep = move
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    code = run_with_out(factory, out, "run", str(spec), "--project", str(factory.project_file))
    assert code == 0, out.getvalue()
    return run_id_of(out.getvalue())


def test_on_a_tty_the_tree_is_redrawn_each_tick_so_the_time_moves_without_events(factory: Harness) -> None:
    out = Tty()

    waiting_run(factory, out)

    times = re.findall(r"active, implement round 1, attempt 1  (\d+s)", out.getvalue())
    assert len(set(times)) >= 3, times


def test_without_a_tty_the_tree_is_printed_only_on_a_change(factory: Harness) -> None:
    out = io.StringIO()

    waiting_run(factory, out)

    waiting = [line for line in out.getvalue().splitlines() if line.startswith("  ~ t1           implement ")]
    assert len(waiting) == len(set(waiting)) == 1
