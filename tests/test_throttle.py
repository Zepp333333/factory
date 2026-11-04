import contextlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from factory.adapters.claude_code import WINDOW_SECONDS
from factory.pipeline.throttle import Brake, Throttle, WindowUsage
from factory.ports.kind import Kind
from factory.run.run_context import TICK_SECONDS
from harness import AgentScript, Harness, ScriptedExecutor, run_id_of, ticket_text
from test_commands import Owner, started

HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})
TICKETS = {"t1": ticket_text("one"), "t2": ticket_text("two", ["t1"])}


def spend_on_first_attempt(factory: Harness, window: str, before: float, after: float, length: float) -> None:
    """Has the window used `before` of it at the start with `length` seconds left, and `after` once t1 starts."""
    resets_at = factory.clock.now() + timedelta(seconds=length)
    seconds = WINDOW_SECONDS[window]
    factory.executor.window_usage = {window: WindowUsage(before, resets_at, seconds)}

    def spend(request: object) -> None:
        if len(factory.executor.requests) == 1:
            factory.executor.window_usage = {window: WindowUsage(after, resets_at, seconds)}

    factory.executor.on_start = spend


def release_when(factory: Harness, owner: Owner, answered: int) -> None:
    """Opens the gate the Attempts wait at once the Owner's first `answered` commands are done."""
    gate = factory.root / "go"
    owner_sleep = factory.clock.on_sleep

    def release(seconds: float) -> None:
        assert owner_sleep is not None
        owner_sleep(seconds)
        if len(owner.procs) >= answered and all(proc.poll() is not None for proc in owner.procs[:answered]):
            gate.touch()

    factory.clock.on_sleep = release
    factory.executor.scripts["implement"] = AgentScript(files=HELLO.files, output=HELLO.output, wait_for=gate)


def test_usage_above_the_cap_starts_no_new_attempts_until_the_window_resets(factory: Harness) -> None:
    factory.project["throttle"] = {"five_hour": {"cap": 0.5}}
    spend_on_first_attempt(factory, "five_hour", 0.3, 0.6, 300)
    factory.executor.scripts["implement"] = HELLO
    seen: list[str] = []

    def look(seconds: float) -> None:
        """Reads the status while braked, and again once the brake reads as journaled before the upgrade, as text."""
        if seconds > TICK_SECONDS and not seen:
            [run_id] = factory.run_ids()
            seen.append(factory.cli("status", run_id).out)
            with contextlib.closing(sqlite3.connect(factory.home / "factory.db")) as db:
                db.execute("UPDATE brakes SET note = 'braked by five_hour at 60%, above its cap 50%'")
                db.commit()
            seen.append(factory.cli("status", run_id).out)

    factory.clock.on_sleep = look
    spec = factory.write_spec("Greeting", "Say hello.", TICKETS)
    resets_at = factory.clock.now() + timedelta(seconds=300)

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [on] = factory.events(run_id, "ThrottleOn")
    # A bare five_hour of the project file is the window of claude-code, as before the brakes named their Executor.
    assert on == {
        "window": "claude-code.five_hour",
        "kind": "cap",
        "used": 0.6,
        "limit": 0.5,
        "until": resets_at.isoformat(),
    }
    [off] = factory.events(run_id, "ThrottleOff")
    assert off == {"window": "claude-code.five_hour", "used": None, "reason": "window reset"}
    # The running Attempt goes to the end, and only the next one waits for the window.
    types = [type for type, _ in factory.all_events(run_id)]
    first_end = types.index("AttemptFinished")
    assert types.index("AttemptStarted") < types.index("ThrottleOn") < first_end < types.index("ThrottleOff")
    assert types.index("ThrottleOff") < types.index("AttemptStarted", first_end)
    started_at = factory.times(run_id, "AttemptStarted")
    assert started_at[1] >= resets_at
    assert factory.times(run_id, "ThrottleOff") == [started_at[1]]
    [first, second] = factory.events(run_id, "AttemptFinished")
    assert (first["outcome"], second["outcome"]) == ("done", "done")
    assert [json.loads(r.brief.read_text())["round"] for r in factory.executor.requests] == [1, 1]
    during, upgraded = seen
    note = f"braked by claude-code.five_hour at 60%, above its cap 50% until {resets_at.isoformat()}"
    assert note in during.splitlines()[0]
    assert note in upgraded.splitlines()[0]
    assert note in result.out
    assert "braked" not in factory.cli("status", run_id).out


def test_usage_ahead_of_the_pace_waits_until_the_window_catches_up(factory: Harness) -> None:
    factory.project["throttle"] = {"five_hour": {"pace": 0.4}}
    spend_on_first_attempt(factory, "five_hour", 0.2, 0.21, 5 * 3600)
    factory.executor.scripts["implement"] = HELLO
    spec = factory.write_spec("Greeting", "Say hello.", TICKETS)
    until = factory.clock.now() + timedelta(seconds=450)

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [on] = factory.events(run_id, "ThrottleOn")
    assert (on["window"], on["kind"], on["until"]) == ("claude-code.five_hour", "pace", until.isoformat())
    assert 0.2 <= on["limit"] < 0.21
    [off] = factory.events(run_id, "ThrottleOff")
    assert off["reason"] == "within bounds"
    [released] = factory.times(run_id, "ThrottleOff")
    assert until <= released
    assert factory.times(run_id, "AttemptStarted")[1] == released
    assert [e["attempt"] for e in factory.events(run_id, "AttemptStarted")] == [1, 1]


def test_a_run_that_first_sees_a_window_late_gets_no_pace_for_the_time_before() -> None:
    now = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    # Two thirds of the window have passed when the Run first sees it.
    resets_at = now + timedelta(seconds=WINDOW_SECONDS["five_hour"] / 3)
    brakes = {"five_hour": Brake(pace=0.6)}
    throttle = Throttle()

    first = throttle.check(brakes, {"five_hour": WindowUsage(0.5, resets_at, WINDOW_SECONDS["five_hour"])}, now)
    braked = throttle.check(brakes, {"five_hour": WindowUsage(0.51, resets_at, WINDOW_SECONDS["five_hour"])}, now)

    assert first is None
    assert braked is not None
    assert (braked.kind, braked.until) == ("pace", now + timedelta(seconds=300))
    assert 0.5 <= braked.limit < 0.51


def test_limits_reject_a_bad_window_or_share_set_a_cap_and_lift_the_brake_on_the_go(factory: Harness) -> None:
    # The Run starts without a throttle, so it ignores the usage until the Owner sets a cap.
    # A day left in the window keeps the brake on until the Owner lifts it, long before the window resets.
    spend_on_first_attempt(factory, "seven_day", 0.3, 0.4, 86400)
    owner = Owner(factory).then(started(factory, "implement"), "limits", "--cap", "hour=0.5")
    owner.then(lambda run_id: len(factory.commands()) == 1, "limits", "--pace", "five_hour=1.5")
    owner.then(lambda run_id: len(factory.commands()) == 2, "limits", "--cap", "seven_day=0.35")
    owner.then(lambda run_id: bool(factory.events(run_id, "ThrottleOn")), "limits", "--no-throttle", "seven_day")
    release_when(factory, owner, 3)
    spec = factory.write_spec("Greeting", "Say hello.", TICKETS)
    resets_at = factory.clock.now() + timedelta(seconds=86400)

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [(code1, _, err1), (code2, _, err2), capped, lifted] = owner.results()
    assert code1 == code2 == 1
    assert "hour: must be <executor>.<window>, or five_hour or seven_day of claude-code" in err1
    assert "five_hour: pace must be a share from 0 to 1" in err2
    assert (capped, lifted) == (
        (0, "Limits set: claude-code.seven_day cap 0.35", ""),
        (0, "Limits set: claude-code.seven_day brake lifted", ""),
    )
    [on] = factory.events(run_id, "ThrottleOn")
    assert (on["window"], on["kind"], on["limit"]) == ("claude-code.seven_day", "cap", 0.35)
    assert factory.events(run_id, "ThrottleOff")[0]["reason"] == "within bounds"
    assert factory.times(run_id, "AttemptStarted")[1] < resets_at
    assert [e["throttle"] for e in factory.events(run_id, "LimitsChanged")] == [
        {"claude-code.seven_day": {"cap": 0.35}},
        {"claude-code.seven_day": None},
    ]


@pytest.mark.parametrize(
    ("throttle", "message"),
    [
        (
            {"hour": {"cap": 0.5}},
            "throttle.hour: must be <executor>.<window>, or five_hour or seven_day of claude-code",
        ),
        ({"five_hour": {"cap": 1.5}}, "throttle.five_hour: cap must be a share from 0 to 1"),
        ({"five_hour": {"pace": -0.1}}, "throttle.five_hour: pace must be a share from 0 to 1"),
        ({"five_hour": {"pace": "fast"}}, "throttle.five_hour: pace must be a share from 0 to 1"),
        ({"five_hour": {}}, "throttle.five_hour: must set cap, pace or both"),
        ({"five_hour": {"cap": 0.5, "rate": 1}}, "throttle.five_hour: unknown field rate, only cap and pace"),
        ({"five_hour": 0.5}, "throttle.five_hour: must be a mapping with cap, pace or both"),
        ([], "throttle: must be a mapping"),
        ({"codex.primary": {"cap": 0.5}}, "throttle.codex.primary: names no Executor, must be one of claude-code"),
    ],
)
def test_check_validates_the_throttle(factory: Harness, throttle: object, message: str) -> None:
    factory.project["throttle"] = throttle

    result = factory.check()

    assert result.code != 0
    assert message in result.err + result.out


def test_check_accepts_a_throttle_with_both_shares(factory: Harness) -> None:
    factory.project["throttle"] = {"five_hour": {"cap": 0.5, "pace": 0.4}, "seven_day": {"cap": 0.6}}

    assert factory.check().code == 0


def fake_executor(factory: Harness, windows: tuple[str, ...]) -> ScriptedExecutor:
    """Has the project run the Executor kind `fake`, which may report `windows`, besides `claude-code`."""
    fake = ScriptedExecutor()
    fake.name = "fake"
    fake.reported_windows = windows
    factory.registries.executors.register("fake", Kind(lambda settings, tools: fake))
    factory.project["executor"] = "fake"
    return fake


def test_an_executor_with_a_window_of_its_own_is_braked_by_the_cap_on_that_window(factory: Harness) -> None:
    fake = fake_executor(factory, ("primary",))
    factory.project["throttle"] = {"fake.primary": {"cap": 0.8}}
    resets_at = factory.clock.now() + timedelta(seconds=600)
    fake.window_usage = {"primary": WindowUsage(0.3, resets_at, 300 * 60)}

    def spend(request: object) -> None:
        fake.window_usage = {"primary": WindowUsage(0.9, resets_at, 300 * 60)}

    fake.on_start = spend
    fake.scripts["implement"] = HELLO
    spec = factory.write_spec("Greeting", "Say hello.", TICKETS)

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [on] = factory.events(run_id, "ThrottleOn")
    assert (on["window"], on["kind"], on["used"], on["limit"]) == ("fake.primary", "cap", 0.9, 0.8)
    assert factory.times(run_id, "AttemptStarted")[1] >= resets_at
    assert "braked by fake.primary at 90%, above its cap 80%" in result.out


def test_the_pace_spreads_over_the_length_the_window_reports() -> None:
    now = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    # Half of a window of 300 minutes is left, and a pace of the whole window allows half of it by now.
    resets_at = now + timedelta(minutes=150)
    brakes = {"fake.primary": Brake(pace=1.0)}
    throttle = Throttle()
    throttle.check(brakes, {"fake.primary": WindowUsage(0.5, resets_at, 300 * 60)}, now)

    braked = throttle.check(brakes, {"fake.primary": WindowUsage(0.6, resets_at, 300 * 60)}, now)

    assert braked is not None
    assert (braked.kind, braked.until) == ("pace", now + timedelta(minutes=30))


def test_check_accepts_a_window_the_executor_may_report_and_warns_of_one_it_never_reports(factory: Harness) -> None:
    fake_executor(factory, ("primary",))
    factory.project["throttle"] = {"fake.primary": {"cap": 0.8}, "fake.weekly": {"cap": 0.5}, "five_hour": {"cap": 1}}

    result = factory.check()

    assert result.code == 0, result.err
    assert result.err.splitlines() == [
        "factory: warning: throttle.fake.weekly: the Executor fake never reports the window weekly, only primary"
    ]
