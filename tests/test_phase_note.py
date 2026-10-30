"""The phase line of the Run tree, `factory status` and the Orca record, once no Ticket works."""

import base64
import contextlib
import io
import re
from dataclasses import replace
from datetime import timedelta

from factory.cli import main
from factory.journal import Journal
from factory.run_view import render_records
from harness import Harness, run_id_of
from test_ci_wait import PASS, RED, RUNNING, Answerer, github
from test_describer import DESCRIPTION, publish, with_describer


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def run_with_out(factory: Harness, out: io.StringIO, url: str) -> str:
    machine = factory.machine()
    with contextlib.redirect_stdout(out):
        code = main(["run", url, "--project", str(factory.project_file)], machine)
    assert code == 0, out.getvalue()
    return run_id_of(out.getvalue())


def ci_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip().startswith("ci: ")]


def test_on_a_tty_the_ci_line_names_the_commit_the_pr_and_a_time_that_ticks(factory: Harness) -> None:
    url = github(factory)
    factory.gh.checks([RUNNING], [RUNNING], [PASS])
    out = Tty()

    run_id = run_with_out(factory, out, url)

    short = factory.git("rev-parse", f"factory/{run_id}")[:7]
    lines = ci_lines(out.getvalue())
    assert all(line.startswith(f"ci: waits for CI on {short}, PR #1, ") for line in lines)
    assert len({line.rsplit(", ", 1)[1] for line in lines}) >= 2, lines


def test_without_a_tty_the_ci_line_appears_once_and_carries_no_time(factory: Harness) -> None:
    url = github(factory)
    factory.gh.checks([RUNNING], [RUNNING], [PASS])

    result = factory.run(url)

    run_id = run_id_of(result.out)
    short = factory.git("rev-parse", f"factory/{run_id}")[:7]
    assert ci_lines(result.out) == [f"ci: waits for CI on {short}, PR #1"]


def test_a_red_ci_says_the_run_waits_for_the_owner_and_how_to_answer(factory: Harness) -> None:
    url = github(factory)
    factory.gh.checks([RED])
    factory.gh.log(91, "FAILED")
    Answerer(factory, "accept")
    out = Tty()
    statuses: list[str] = []
    answer = factory.clock.on_sleep
    assert answer is not None

    def look(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "DecisionWaiting") and not statuses:
            statuses.append(factory.cli("status", factory.run_ids()[-1]).out)
        answer(seconds)

    factory.clock.on_sleep = look

    run_id = run_with_out(factory, out, url)

    short = factory.git("rev-parse", f"factory/{run_id}")[:7]
    expected = re.compile(
        rf"ci: waits for you: CI failed on {short} after \d+s, "
        rf"answer with `factory decide {run_id} @run recheck` or `accept`"
    )
    assert any(expected.fullmatch(line) for line in ci_lines(out.getvalue())), out.getvalue()
    assert any(expected.fullmatch(line) for line in statuses[0].splitlines()), statuses[0]


def test_a_timed_out_ci_says_the_run_waits_for_the_owner_and_how_to_answer(factory: Harness) -> None:
    url = github(factory)
    factory.project["ci"] = {"timeout": "10m"}
    factory.gh.checks([RUNNING])
    Answerer(factory, "accept")

    result = factory.run(url)

    run_id = run_id_of(result.out)
    short = factory.git("rev-parse", f"factory/{run_id}")[:7]
    assert (
        f"ci: waits for you: CI timed out on {short}, answer with `factory decide {run_id} @run recheck` or `accept`"
        in ci_lines(result.out)
    )


def test_after_recheck_the_time_counts_from_the_new_ci_started(factory: Harness) -> None:
    url = github(factory)
    fixed = "f" * 40
    factory.gh.checks([RED])
    factory.gh.log(91, "FAILED")
    factory.gh.checks([RUNNING], [RUNNING], [PASS], sha=fixed)
    Answerer(factory, "recheck")
    answer = factory.clock.on_sleep
    assert answer is not None
    seen: list[str] = []

    def push_a_fix(seconds: float) -> None:
        run_ids = factory.run_ids()
        if run_ids and factory.events(run_ids[-1], "DecisionWaiting"):
            factory.gh.set(head_oid=fixed)
        if run_ids and len(factory.events(run_ids[-1], "CiStarted")) == 2:
            seen.append(factory.cli("status", run_ids[-1]).out)
        answer(seconds)

    factory.clock.on_sleep = push_a_fix

    run_with_out(factory, io.StringIO(), url)

    first = ci_lines(seen[0])
    assert first and re.fullmatch(r"ci: waits for CI on fffffff, PR #1, \d+s", first[0]), seen[0]


def test_the_orca_record_of_the_run_carries_the_same_text(factory: Harness) -> None:
    url = github(factory)
    factory.gh.checks([RUNNING], [PASS])
    texts: list[str] = []

    def look(seconds: float) -> None:
        run_ids = factory.run_ids()
        if run_ids and factory.events(run_ids[-1], "CiStarted") and not texts:
            journal = Journal(factory.machine().journal_path, factory.clock)
            try:
                texts.append(render_records(journal, run_ids[-1], factory.clock.now()))
            finally:
                journal.close()

    factory.clock.on_sleep = look

    result = factory.run(url)

    run_id = run_id_of(result.out)
    short = factory.git("rev-parse", f"factory/{run_id}")[:7]
    msgs = [base64.b64decode(m).decode() for m in re.findall(r"msg=([A-Za-z0-9+/=]+)", texts[0])]
    expected = re.compile(rf"ci: waits for CI on {short}, PR #1, \d+s")
    assert any(expected.fullmatch(msg) for msg in msgs), msgs


def test_a_finished_run_shows_no_phase_line(factory: Harness) -> None:
    url = github(factory)
    factory.gh.checks([PASS])
    out = Tty()

    run_id = run_with_out(factory, out, url)

    assert ci_lines(factory.cli("status", run_id).out) == []
    last_tree = out.getvalue().rsplit("\x1b[J", 1)[-1]
    assert ci_lines(last_tree) == [], last_tree


def test_on_a_tty_the_describe_line_counts_from_describer_started(factory: Harness) -> None:
    gate = factory.root / "go"
    with_describer(factory)
    factory.executor.scripts["describe"] = replace(DESCRIPTION, wait_for=gate)
    sleeps: list[int] = []

    def move(_: float) -> None:
        run_ids = factory.run_ids()
        if not run_ids or not factory.events(run_ids[-1], "DescriberStarted"):
            return
        sleeps.append(1)
        factory.clock._now += timedelta(seconds=5)
        if len(sleeps) == 3:
            gate.touch()

    factory.clock.on_sleep = move
    out = Tty()

    run_with_out(factory, out, publish(factory))

    lines = [line.strip() for line in out.getvalue().splitlines() if line.strip().startswith("describe: ")]
    text = "describe: the Describer writes the PR text"
    assert lines[:2] == [f"{text}, 5s", f"{text}, 10s"]
