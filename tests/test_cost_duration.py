"""Cost and duration of every node: what the Journal stores and what the Run's output shows."""

import contextlib
import io
import os
import re
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from factory.cli import main
from harness import AgentScript, Harness, clock_text, run_id_of, ticket_text
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
            clock.sleep(seconds)
            gate.touch()

    clock.on_sleep = jump


def query(factory: Harness, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    with contextlib.closing(sqlite3.connect(factory.home / "factory.db")) as db:
        return db.execute(sql, params).fetchall()


def test_every_node_writes_its_duration_only_stages_have_a_cost_and_the_tree_shows_both_so_far(
    factory: Harness,
) -> None:
    gate = factory.root / "go"
    deciding(factory, RISK)
    factory.pipeline["gates"] = {
        "check": {"kind": "command", "with": {"run": "true"}, "next": {"pass": "review", "fail": "abandoned"}}
    }
    factory.pipeline["stages"]["implement"]["next"]["done"] = "check"
    factory.executor.scripts["implement"] = DONE
    factory.executor.scripts["review"] = replace(review("two_way", "module"), cost=0.1, wait_for=gate)
    jump_when_asked(factory, gate, 125)
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

    # The tree shows the time since the start, and while `review` runs its Attempt is not in the sum yet.
    lines = result.out.splitlines()
    assert any(re.fullmatch(r"t1 +review +active +review +1 +1 +\d+s +\$0\.42", line) for line in lines)
    took = (factory.times(run_id, "RunFinished")[0] - factory.times(run_id, "RunStarted")[0]).total_seconds()
    assert took >= 125
    assert f"Run {run_id}  [done]  factory/{run_id}  {clock_text(took)}  $0.52" in lines
    assert f"Run {run_id} finished: done in {clock_text(took)}, $0.52" in lines
    ticket = [line for line in lines if line.startswith("t1")][-1]
    assert re.fullmatch(rf"t1 +done +merged +judge +1 +0 +{factory.ticket_took(run_id, 't1')} +\$0\.52", ticket)


def test_every_attempt_writes_its_usage_or_unknown_and_its_duration_and_status_and_the_projection_sum_them(
    factory: Harness,
) -> None:
    gate = factory.root / "go"
    factory.executor.scripts["implement"] = DONE
    factory.executor.ticket_scripts[("t1", "implement")] = replace(DONE, wait_for=gate)
    jump_when_asked(factory, gate, 90)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET, "t2": ticket_text("Greet again")})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    finished = factory.events(run_id, "AttemptFinished")[0]
    assert finished["ticket"] == "t1"
    assert finished["usage"] == {
        "cost_usd": 0.42,
        "cost_source": "reported",
        "turns": 7,
        **DONE_TOKENS,
        "thinking_tokens": None,
    }
    # The Run ticks on until the agent exits, and every tick moves the clock, so only the jump bounds the duration.
    assert finished["duration"] >= 90
    started_at = factory.times(run_id, "AttemptStarted")[0]
    finished_at = factory.times(run_id, "AttemptFinished")[0]
    assert finished["duration"] == (finished_at - started_at).total_seconds()

    lines = factory.cli("status", run_id).out.splitlines()
    assert lines[0] == f"Run {run_id}  done  factory/{run_id}  {factory.run_took(run_id)}  $0.84"
    assert lines[1].split()[-2:] == ["DURATION", "COST"]
    for row, ticket in zip(lines[2:4], ("t1", "t2"), strict=True):
        assert re.fullmatch(rf"{ticket} .* {factory.ticket_took(run_id, ticket)} +\$0\.42", row), row

    other = factory.root / "other"
    (other / "tickets").mkdir(parents=True)
    (other / "spec.md").write_text("# Other\n\nAnother.\n")
    (other / "tickets" / "t3.md").write_text(ticket_text("Greet once more"))
    factory.pipeline["stages"]["implement"]["attempts"] = 3
    ran_out = AgentScript(fail="max_turns", turns=3, cost=0.3, tokens=(1, 1, 1, 1))
    factory.executor.ticket_scripts[("t3", "implement")] = [ran_out, AgentScript(fail="crash"), DONE]
    factory.clock.sleep(1)

    result = factory.run(other)

    # A failed Attempt with a result writes its usage, and one without a result writes it as unknown, not zero.
    assert result.code == 0, result.out
    other_id = run_id_of(result.out)
    failed, crashed, done = factory.events(other_id, "AttemptFinished")
    assert failed["outcome"] == "failed"
    assert failed["usage"] == {
        "cost_usd": 0.3,
        "cost_source": "reported",
        "turns": 3,
        "input_tokens": 2,
        "output_tokens": 3,
        "cache_read_tokens": 4,
        "cache_write_tokens": 5,
        "thinking_tokens": None,
    }
    assert crashed["cause"] == "executor_crash"
    assert crashed["usage"] is None
    assert "duration" in failed and "duration" in crashed
    assert done["usage"]["cost_usd"] == 0.42
    costs = query(factory, "SELECT attempt, cost_usd FROM attempts WHERE run_id = ? ORDER BY attempt", other_id)
    assert costs == [(1, 0.3), (2, None), (3, 0.42)]

    # Sums over Attempts of unknown cost show it.
    assert f"Run {other_id} finished: done in {factory.run_took(other_id)}, $0.72+?" in result.out.splitlines()
    status = factory.cli("status", other_id).out.splitlines()
    assert status[0].endswith("$0.72+?")
    assert status[2].split()[-1] == "$0.72+?"

    # The projection gives the price of a Stage across Runs, over the Attempts whose cost is known.
    rows = query(
        factory,
        "SELECT node, count(*), round(avg(cost_usd), 2), round(sum(cost_usd), 2) FROM attempts GROUP BY node",
    )
    assert rows == [("implement", 5, 0.39, 1.56)]
    columns = query(
        factory,
        "SELECT turns, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
        " started_at IS NOT NULL, finished_at IS NOT NULL, duration IS NOT NULL FROM attempts LIMIT 1",
    )
    assert columns == [(7, 11, 22, 33, 44, 1, 1, 1)]


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

    def move(seconds: float) -> None:
        if seconds == 5:
            return
        sleeps.append(1)
        factory.clock.sleep(5)
        if len(sleeps) == 3:
            gate.touch()

    factory.clock.on_sleep = move
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    code = run_with_out(factory, out, "run", str(spec), "--project", str(factory.project_file))
    assert code == 0, out.getvalue()
    return run_id_of(out.getvalue())


def test_without_a_tty_the_tree_is_printed_only_on_a_change(factory: Harness) -> None:
    out = io.StringIO()

    waiting_run(factory, out)

    waiting = [line for line in out.getvalue().splitlines() if re.match(r"t1 +implement +active ", line)]
    assert len(waiting) == len(set(waiting)) == 1


# The terminal narrows to 40 columns once the Run has drawn this many frames of its tree.
NARROW_AFTER = 7


def test_on_a_tty_the_tree_is_redrawn_each_tick_and_no_row_wraps_when_the_terminal_narrows(
    factory: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = Tty()
    monkeypatch.setattr(
        "factory.views.run_view.shutil.get_terminal_size",
        lambda: os.terminal_size((40 if out.getvalue().count("TOTAL") >= NARROW_AFTER else 200, 24)),
    )

    waiting_run(factory, out)

    # The wide frames show the time move on each tick, though no event happened.
    frames = out.getvalue().split("\x1b[J")
    times = re.findall(r"active +implement +1 +1 +(\d+s)", "".join(frames[:NARROW_AFTER]))
    assert len(set(times)) >= 3, times

    # The first redraw at the narrow width moves up over every row the old tree rewrapped to.
    last_wide = frames[NARROW_AFTER - 1]
    tree = last_wide[last_wide.index("Run ") :].split("\n")[:-1]
    assert any(len(line) > 40 for line in tree), tree
    moved = re.search(r"\x1b\[(\d+)F$", last_wide)
    assert moved, last_wide
    assert int(moved.group(1)) == sum(-(-len(line) // 40) for line in tree)

    # Every later frame fits the terminal, so no row wraps and each redraw replaces the tree.
    rows = [
        line
        for frame in frames[NARROW_AFTER:]
        for line in frame.splitlines()
        if re.match(r"(Run \S+  \[|Ticket |t1 )", line)
    ]
    assert rows and all(len(line) < 40 for line in rows), rows
