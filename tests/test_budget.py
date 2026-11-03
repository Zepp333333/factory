"""A dollar budget caps the cost of a Run and of an Executor's share: at the cap the Run asks the owner."""

from pathlib import Path

import pytest

from harness import AgentScript, Harness, run_id_of, ticket_text
from test_ci_fix import with_ci_fix
from test_ci_wait import RED, Answerer
from test_commands import Owner, started
from test_mixed_executors import with_other
from test_review import REVIEW, with_review

HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"}, cost=1.0)
TICKETS = {"t1": ticket_text("one"), "t2": ticket_text("two", ["t1"])}


def asked(factory: Harness):  # type: ignore[no-untyped-def]
    return lambda run_id: bool(factory.events(run_id, "DecisionWaiting"))


def test_a_run_at_its_budget_starts_no_new_attempt_asks_the_owner_and_goes_on_when_limits_raise_it(
    factory: Harness,
) -> None:
    factory.project["throttle"] = {"budget_usd": 1}
    factory.executor.scripts["implement"] = HELLO
    owner = Owner(factory).then(asked(factory), "status")
    owner.then(lambda run_id: len(owner.procs) == 1, "limits", "--budget-usd", "5")
    spec = factory.write_spec("Greeting", "Say hello.", TICKETS)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [(_, during, _), (code, limits, err)] = owner.results()
    assert "Budget: $1.00 of $1.00" in during
    assert (code, limits, err) == (0, "Limits set: budget $5.00", "")
    [waiting] = factory.events(run_id, "DecisionWaiting")
    assert (waiting["ticket"], waiting["node"], waiting["outcomes"]) == ("@run", "budget_spent", ["finish"])
    card = Path(waiting["card"]).read_text()
    assert "- @run: $1.00 of $1.00" in card
    assert f"factory limits {run_id} --budget-usd" in card
    assert f"factory decide {run_id} @run finish" in card
    [decided] = factory.events(run_id, "DecisionDecided")
    assert (decided["ticket"], decided["node"], decided["outcome"]) == ("@run", "budget_spent", "raise")
    # The second Ticket starts only after the owner raised the budget.
    types = [type for type, _ in factory.all_events(run_id)]
    second = [i for i, type in enumerate(types) if type == "AttemptStarted"][1]
    assert types.index("DecisionWaiting") < types.index("LimitsChanged") < second
    assert factory.events(run_id, "LimitsChanged")[0]["budgets"] == {"@run": 5.0}
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"
    assert "Budget: $2.00 of $5.00" in factory.cli("status", run_id).out


def test_finish_at_the_budget_stops_the_work_abandons_what_did_not_merge_and_lands_what_merged(
    factory: Harness,
) -> None:
    factory.project["throttle"] = {"budget_usd": 1}
    factory.executor.scripts["implement"] = HELLO
    owner = (
        Owner(factory).then(asked(factory), "decide", "@run", "raise").then(asked(factory), "decide", "@run", "finish")
    )
    spec = factory.write_spec("Greeting", "Say hello.", TICKETS)

    result = factory.run(spec)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    [(refused, _, reason), (code, answer, _)] = owner.results()
    assert refused != 0 and "finish" in reason
    assert (code, answer) == (0, "Run decided budget_spent: finish")
    assert [e["ticket"] for e in factory.events(run_id, "AttemptStarted")] == ["t1"]
    assert factory.events(run_id, "RunFinished")[0]["state"] == "partial"
    assert [e["ticket"] for e in factory.events(run_id, "TicketCancelled")] == ["t2"]
    assert factory.events(run_id, "PullRequestRecorded"), "what merged lands"


def test_finish_stops_a_ticket_that_waits_at_a_stage_and_keeps_its_work(factory: Harness) -> None:
    with_review(factory)
    factory.project["throttle"] = {"budget_usd": 1}
    factory.executor.scripts["implement"] = HELLO
    owner = Owner(factory).then(asked(factory), "decide", "@run", "finish")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("one")})

    result = factory.run(spec)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    assert owner.results()[0][0] == 0
    assert [e["node"] for e in factory.events(run_id, "AttemptStarted")] == ["implement"]
    assert factory.events(run_id, "NodeEntered")[-1]["node"] == "abandoned"
    assert "abandoned, its work is in" in factory.cli("status", run_id).out


def test_finish_stops_the_attempt_that_runs_as_cancel_does(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    factory.project["throttle"] = {"budget_usd": 1}
    factory.executor.scripts["implement"] = HELLO
    factory.executor.ticket_scripts[("t2", "implement")] = AgentScript(
        files={"half.txt": "half\n"}, output=HELLO.output, wait_for=factory.root / "never"
    )
    owner = Owner(factory).then(asked(factory), "decide", "@run", "finish")
    tickets = {"t1": ticket_text("one"), "t2": ticket_text("two"), "t3": ticket_text("three")}
    spec = factory.write_spec("Greeting", "Say hello.", tickets)

    result = factory.run(spec)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    assert owner.results()[0][0] == 0
    [lost] = factory.events(run_id, "AttemptLost")
    assert (lost["ticket"], lost["cause"]) == ("t2", "cancelled")
    assert factory.slots() == []
    assert {e["ticket"] for e in factory.events(run_id, "TicketClaimed")} == {"t1", "t2"}
    assert factory.events(run_id, "RunFinished")[0]["state"] == "partial"


def test_a_run_that_spends_its_budget_on_its_last_attempt_asks_nothing_and_ends(factory: Harness) -> None:
    factory.project["throttle"] = {"budget_usd": 1}
    factory.executor.scripts["implement"] = HELLO
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("one")})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert not factory.events(run_id, "DecisionWaiting")
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"


def test_an_executors_budget_caps_its_share_and_limits_raises_it_by_name(factory: Harness) -> None:
    other = with_other(factory)
    factory.project["throttle"] = {"other.budget_usd": 1, "budget_usd": 1000}
    factory.executor.scripts["implement"] = AgentScript(files=HELLO.files, output=HELLO.output, cost=50.0)
    other.scripts["review"] = AgentScript(files={}, output=REVIEW.output, cost=1.0)
    other.scripts["fix"] = other.scripts["review"]
    owner = Owner(factory).then(asked(factory), "status")
    owner.then(lambda run_id: len(owner.procs) == 1, "limits", "--budget-usd", "other=5")
    spec = factory.write_spec("Greeting", "Say hello.", TICKETS)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [(_, during, _), (_, limits, _)] = owner.results()
    # The first Ticket reviewed on `other`, whose budget is then spent, far below the budget of the Run.
    assert "Budget: $51.00 of $1000.00, other $1.00 of $1.00" in during
    assert limits == "Limits set: other budget $5.00"
    [decided] = factory.events(run_id, "DecisionDecided")
    assert decided["outcome"] == "raise"
    assert factory.events(run_id, "LimitsChanged")[0]["budgets"] == {"other": 5.0}
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"


def test_set_gives_the_run_a_budget_and_the_nested_form_of_an_executors_budget_reads_alike(factory: Harness) -> None:
    factory.executor.scripts["implement"] = HELLO
    owner = Owner(factory).then(asked(factory), "status")
    owner.then(lambda run_id: len(owner.procs) == 1, "limits", "--budget-usd", "5", "--budget-usd", "claude-code=5")
    spec = factory.write_spec("Greeting", "Say hello.", TICKETS)

    result = factory.run(spec, "--set", "throttle.budget_usd=3", "--set", "throttle.claude-code.budget_usd=1")

    assert result.code == 0, result.out + result.err
    [(_, during, _), (_, limits, _)] = owner.results()
    assert "Budget: $1.00 of $3.00, claude-code $1.00 of $1.00" in during
    assert limits == "Limits set: budget $5.00, claude-code budget $5.00"


def test_limits_that_leave_a_budget_spent_keep_the_decision_open_and_bad_budgets_are_rejected(factory: Harness) -> None:
    factory.project["throttle"] = {"budget_usd": 1}
    factory.executor.scripts["implement"] = HELLO
    owner = Owner(factory).then(asked(factory), "limits", "--budget-usd", "0.5")
    owner.then(lambda run_id: len(owner.procs) == 1, "limits", "--budget-usd", "0")
    owner.then(lambda run_id: len(owner.procs) == 2, "limits", "--budget-usd", "nobody=5")
    owner.then(lambda run_id: len(owner.procs) == 3, "decide", "@run", "finish")
    spec = factory.write_spec("Greeting", "Say hello.", TICKETS)

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    [lowered, zero, nobody, finish] = owner.results()
    assert lowered[:2] == (0, "Limits set: budget $0.50")
    assert zero[0] == 1 and "budget of @run: must be a positive number of dollars" in zero[2]
    assert nobody[0] == 1 and "nobody is no Executor of the Run, only claude-code" in nobody[2]
    assert finish[0] == 0
    assert [e["budgets"] for e in factory.events(run_id, "LimitsChanged")] == [{"@run": 0.5}]
    assert len(factory.events(run_id, "DecisionWaiting")) == 1


def test_limits_says_what_a_budget_looks_like(factory: Harness) -> None:
    result = factory.cli("limits", "any-run", "--budget-usd", "lots")

    assert result.code == 2
    assert "must be [EXECUTOR=]USD such as 20 or claude-code=5" in result.err


def test_a_budget_lowered_by_limits_under_what_a_run_spent_holds_it_back_too(factory: Harness) -> None:
    factory.executor.scripts["implement"] = HELLO
    owner = Owner(factory).then(started(factory, "implement"), "limits", "--budget-usd", "1")
    owner.then(asked(factory), "limits", "--budget-usd", "5")
    gate = factory.root / "go"
    owner_sleep = factory.clock.on_sleep

    def release(seconds: float) -> None:
        assert owner_sleep is not None
        owner_sleep(seconds)
        if owner.procs and owner.procs[0].poll() is not None:
            gate.touch()

    factory.clock.on_sleep = release
    factory.executor.scripts["implement"] = AgentScript(files=HELLO.files, output=HELLO.output, cost=1.0, wait_for=gate)
    spec = factory.write_spec("Greeting", "Say hello.", TICKETS)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [e["outcome"] for e in factory.events(run_id, "DecisionDecided")] == ["raise"]
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"


@pytest.mark.parametrize(
    ("throttle", "message"),
    [
        ({"budget_usd": 0}, "throttle.budget_usd: must be a positive number of dollars"),
        ({"budget_usd": "lots"}, "throttle.budget_usd: must be a positive number of dollars"),
        ({"budget_usd": True}, "throttle.budget_usd: must be a positive number of dollars"),
        ({"claude-code.budget_usd": -1}, "throttle.claude-code.budget_usd: must be a positive number of dollars"),
        ({"codex.budget_usd": 5}, "throttle.codex.budget_usd: names no Executor, must be one of claude-code"),
        ({"codex": {"budget_usd": 5}}, "throttle.codex.budget_usd: names no Executor, must be one of claude-code"),
        ({"five_hour": {"cap": 0.5, "budget_usd": 5}}, "unknown field budget_usd, only cap and pace"),
    ],
)
def test_check_validates_the_budgets(factory: Harness, throttle: object, message: str) -> None:
    factory.project["throttle"] = throttle

    result = factory.check()

    assert result.code != 0
    assert message in result.err + result.out


def test_check_accepts_budgets_beside_the_brakes(factory: Harness) -> None:
    factory.project["throttle"] = {
        "budget_usd": 20.5,
        "claude-code": {"budget_usd": 5},
        "five_hour": {"cap": 0.5},
    }

    result = factory.check()

    assert result.code == 0, result.err
    assert result.err == ""


def test_a_spent_budget_starts_no_ci_fixer_and_goes_to_the_decision_on_red_ci(factory: Harness) -> None:
    spec = with_ci_fix(factory)
    factory.project["throttle"] = {"budget_usd": 1}
    factory.executor.scripts["implement"] = AgentScript(files=HELLO.files, output=HELLO.output, cost=1.0)
    factory.gh.checks([RED])
    answers = Answerer(factory, "accept")

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    assert not factory.events(run_id, "CiFixStarted")
    assert answers.codes() == [0]
    assert [e["node"] for e in factory.events(run_id, "DecisionWaiting")] == ["ci_failed"]
