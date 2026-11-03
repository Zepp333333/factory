"""A Run mixes Executors, one per Stage: each node runs on the Executor its settings name."""

from datetime import timedelta

from factory.pipeline.throttle import WindowUsage
from factory.ports.kind import Kind
from factory.ports.sandbox import Needs
from harness import AgentScript, Harness, ScriptedExecutor, run_id_of, ticket_text
from test_check import PairedEfforts, errors_of
from test_review import REVIEW, with_review

HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})


class OtherExecutor(ScriptedExecutor):
    """OtherExecutor is a second fake Executor, with a name, hosts, an env and an agent of its own."""

    name = "other"
    reported_windows: tuple[str, ...] = ("primary",)

    def needs(self) -> Needs:
        return Needs(egress=("api.other.test",), env={"OTHER_HOME": "/home/agent/other"}, agent=("other",))


def with_other(factory: Harness) -> OtherExecutor:
    """Registers the Executor kind `other` beside `claude-code` and has the Stage `review` run on it."""
    other = OtherExecutor()
    factory.registries.executors.register("other", Kind(lambda settings, tools: other))
    with_review(factory)
    factory.pipeline["stages"]["review"]["executor"] = "other"
    del factory.executor.scripts["review"]
    other.scripts["review"] = REVIEW
    return other


def test_a_run_runs_implement_on_one_executor_and_review_on_another_in_the_same_ticket_sandbox(
    factory: Harness,
) -> None:
    other = with_other(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("one")})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [request.stage for request in factory.executor.requests] == ["implement", "describe"]
    assert [request.stage for request in other.requests] == ["review"]
    [implement, _] = factory.executor.requests
    [review] = other.requests
    assert implement.sandbox == review.sandbox
    assert (implement.settings.executor, review.settings.executor) == ("claude-code", "other")
    # The Run assembles one kit per Executor it uses, each in a folder of its own.
    kits = factory.run_dir(run_id) / "kit"
    assert (implement.kit, review.kit) == (kits / "claude-code", kits / "other")
    assert (kits / "other").is_dir()
    started = factory.events(run_id, "AttemptStarted")
    assert [(e["node"], e["executor"]) for e in started] == [("implement", "claude-code"), ("review", "other")]
    # The Sandbox gets what every Executor of the Pipeline needs.
    [run_started] = factory.events(run_id, "RunStarted")
    assert "api.other.test" in run_started["sandbox_egress"]
    assert "api.anthropic.com" in run_started["sandbox_egress"]
    assert "OTHER_HOME" in run_started["sandbox_env"]
    assert factory.sandbox.needs.agent == ("claude", "other")


def test_stats_groups_the_attempts_by_executor(factory: Harness) -> None:
    with_other(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("one")})
    run = factory.run(spec)
    assert run.code == 0, run.err

    result = factory.cli("stats", run_id_of(run.out), "--by", "executor")

    assert result.code == 0, result.err
    lines = result.out.splitlines()
    assert any(line.startswith("implement") and "claude-code" in line for line in lines), result.out
    assert any(line.startswith("review") and "other" in line for line in lines), result.out


def test_a_braked_executor_holds_back_only_the_stages_that_run_on_it(factory: Harness) -> None:
    other = with_other(factory)
    factory.project["throttle"] = {"five_hour": {"cap": 0.5}}
    resets_at = factory.clock.now() + timedelta(seconds=600)
    factory.executor.window_usage = {"five_hour": WindowUsage(0.3, resets_at, 5 * 3600)}

    def spend(request: object) -> None:
        factory.executor.window_usage = {"five_hour": WindowUsage(0.6, resets_at, 5 * 3600)}

    factory.executor.on_start = spend
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("one"), "t2": ticket_text("two", ["t1"])})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [on] = factory.events(run_id, "ThrottleOn")
    assert on["window"] == "claude-code.five_hour"
    started = factory.events(run_id, "AttemptStarted")
    times = factory.times(run_id, "AttemptStarted")
    nodes = [(e["ticket"], e["node"]) for e in started]
    assert nodes[:4] == [("t1", "implement"), ("t1", "review"), ("t2", "implement"), ("t2", "review")]
    # The review of t1 runs on the other Executor while claude-code is braked, and t2's implement waits for the reset.
    assert times[1] < resets_at <= times[2]
    assert len(other.requests) == 2


def test_check_accepts_any_registered_executor_on_a_node_and_checks_its_model_against_that_executor(
    factory: Harness,
) -> None:
    factory.registries.executors.register("paired", Kind(lambda settings, tools: PairedEfforts()))
    factory.pipeline["stages"]["implement"]["executor"] = "paired"

    assert factory.check().code == 0

    factory.pipeline["stages"]["implement"]["model"] = "sonnet"
    factory.pipeline["describer"] = {"role": "describer", "executor": "paired", "effort": "max"}

    assert sorted(errors_of(factory)) == [
        "describer.effort: max is not an effort of the model small; use max only with big",
        "stages.implement.model: sonnet is not a model of the paired Executor; use small or big",
    ]


def test_the_status_shows_the_brake_of_one_executor_that_still_holds_once_the_other_lets_go(factory: Harness) -> None:
    other = with_other(factory)
    factory.project["throttle"] = {"five_hour": {"cap": 0.5}, "other.primary": {"cap": 0.5}}
    now = factory.clock.now()
    claude_resets, other_resets = now + timedelta(seconds=3600), now + timedelta(seconds=300)
    factory.executor.window_usage = {"five_hour": WindowUsage(0.3, claude_resets, 5 * 3600)}
    other.window_usage = {"primary": WindowUsage(0.9, other_resets, 5 * 3600)}

    def spend(request: object) -> None:
        factory.executor.window_usage = {"five_hour": WindowUsage(0.6, claude_resets, 5 * 3600)}

    factory.executor.on_start = spend
    seen: list[str] = []

    def look(seconds: float) -> None:
        [run_id] = factory.run_ids()
        if not seen and factory.events(run_id, "ThrottleOff"):
            seen.append(factory.cli("status", run_id).out)

    factory.clock.on_sleep = look
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("one")})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [e["window"] for e in factory.events(run_id, "ThrottleOn")] == ["other.primary", "claude-code.five_hour"]
    assert [e["window"] for e in factory.events(run_id, "ThrottleOff")][0] == "other.primary"
    # The review on the other Executor waits for its window, though claude-code is still braked.
    review = factory.times(run_id, "AttemptStarted")[1]
    assert other_resets <= review < claude_resets
    [status] = seen
    assert "braked by claude-code.five_hour at 60%, above its cap 50%" in status.splitlines()[0]
