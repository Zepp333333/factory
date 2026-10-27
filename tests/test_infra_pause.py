import subprocess
from pathlib import Path

from harness import AgentScript, Harness, run_id_of, ticket_text

CRASH = AgentScript(fail="crash")
AUTH = AgentScript(fail="auth")


def done(name: str) -> AgentScript:
    return AgentScript(files={f"{name}.txt": f"{name}\n"}, output={"summary": f"Add {name}", "handoff": "done"})


class Answerer:
    """An Answerer answers the Run-level Decision with `factory decide` from the Run's sleep, once it waits."""

    def __init__(self, factory: Harness, outcome: str) -> None:
        self.factory = factory
        self.outcome = outcome
        self.proc: subprocess.Popen[str] | None = None
        factory.clock.on_sleep = self.on_sleep

    def on_sleep(self, seconds: float) -> None:
        if self.proc is not None or not self.factory.run_ids():
            return
        run_id = self.factory.run_ids()[-1]
        if self.factory.events(run_id, "DecisionWaiting"):
            self.proc = self.factory.spawn("decide", run_id, "@run", self.outcome)

    def output(self) -> tuple[int | None, str]:
        assert self.proc is not None
        out, _ = self.proc.communicate(timeout=30)
        return self.proc.returncode, out


def two_tickets(factory: Harness) -> Path:
    factory.project["max_parallel"] = 2
    factory.pipeline["stages"]["implement"]["attempts"] = 2
    return factory.write_spec("Pair", "Two.", {"t1": ticket_text("t1"), "t2": ticket_text("t2")})


def test_two_tickets_failing_alike_open_a_run_decision_and_retry_spends_no_attempt(factory: Harness) -> None:
    spec = two_tickets(factory)
    factory.executor.scripts["implement"] = [CRASH, done("x")]
    factory.executor.ticket_scripts[("t1", "implement")] = [CRASH, CRASH, done("t1")]
    factory.executor.ticket_scripts[("t2", "implement")] = [CRASH, done("t2")]
    answerer = Answerer(factory, "retry")

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [waiting] = factory.events(run_id, "DecisionWaiting")
    assert (waiting["ticket"], waiting["outcomes"]) == ("@run", ["retry", "abort"])
    card = Path(waiting["card"]).read_text()
    assert "executor_crash" in card and "t1" in card and "t2" in card
    assert f"factory decide {run_id} @run <outcome>" in card
    assert "waiting for you at infra_failure" in result.out
    code, out = answerer.output()
    assert code == 0
    assert out.strip() == "Run decided infra_failure: retry"
    assert [e["outcome"] for e in factory.events(run_id, "DecisionDecided")] == ["retry"]
    assert factory.git("show", f"factory/{run_id}:t1.txt") == "t1"
    assert factory.git("show", f"factory/{run_id}:t2.txt") == "t2"
    assert factory.waiting() == []
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]


def test_abort_ends_the_run_as_aborted_and_starts_no_more_attempts(factory: Harness) -> None:
    spec = two_tickets(factory)
    factory.executor.scripts["implement"] = AUTH
    answerer = Answerer(factory, "abort")

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    [waiting] = factory.events(run_id, "DecisionWaiting")
    assert "auth" in Path(waiting["card"]).read_text()
    code, out = answerer.output()
    assert code == 0
    assert out.strip() == "Run decided infra_failure: abort"
    assert f"Run {run_id} finished: aborted" in result.out
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["aborted"]
    assert factory.cli("status", run_id).out.splitlines()[0].startswith(f"Run {run_id}  aborted")
    assert factory.slots() == []
    assert len(factory.executor.requests) <= 4


def test_failures_of_one_ticket_or_of_different_causes_open_no_decision(factory: Harness) -> None:
    spec = two_tickets(factory)
    factory.pipeline["stages"]["implement"]["attempts"] = 3
    factory.executor.ticket_scripts[("t1", "implement")] = [CRASH, CRASH, done("t1")]
    factory.executor.ticket_scripts[("t2", "implement")] = [AUTH, done("t2")]
    factory.executor.scripts["implement"] = done("x")

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "DecisionWaiting") == []
    causes = sorted(e["cause"] for e in factory.events(run_id, "AttemptFinished") if "cause" in e)
    assert causes == ["auth", "executor_crash", "executor_crash"]
