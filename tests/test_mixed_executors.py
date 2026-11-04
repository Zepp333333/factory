"""A Run mixes Executors, one per node: each node runs on the Executor its settings name."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from factory.adapters.claude_code import WINDOW_SECONDS
from factory.config import load_project
from factory.core.events import Event
from factory.journal.journal import EventRow
from factory.journal.recovery import replay
from factory.pipeline.pipeline import Pipeline
from factory.pipeline.throttle import Brake, WindowUsage
from factory.ports.executor import AttemptRequest
from factory.ports.kind import CheckContext, Kind, Problems, Report
from factory.ports.sandbox import Needs, Sandbox
from harness import AgentScript, Harness, ScriptedExecutor, ScriptedHandle, run_id_of, ticket_text
from test_check import PairedEfforts, errors_of
from test_commands import Owner, started
from test_review import REVIEW, with_review
from test_throttle import release_when
from test_wiring import errors_of as project_errors_of

HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})


class OtherExecutor(ScriptedExecutor):
    """OtherExecutor is a second fake Executor, with a name, hosts, an env and an agent of its own."""

    name = "other"
    reported_windows: tuple[str, ...] = ("primary",)

    def needs(self) -> Needs:
        return Needs(egress=("api.other.test",), env={"OTHER_HOME": "/home/agent/other"}, agent=("other",))


def five_hour(used: float, resets_at: datetime) -> dict[str, WindowUsage]:
    """Returns the windows of an Executor whose five-hour window is used up to `used`."""
    return {"five_hour": WindowUsage(used, resets_at, WINDOW_SECONDS["five_hour"])}


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
    # `factory stats --by executor` groups the Attempts by the Executor of their fingerprint.
    stats = factory.cli("stats", run_id, "--by", "executor")
    assert stats.code == 0, stats.err
    lines = stats.out.splitlines()
    assert any(line.startswith("implement") and "claude-code" in line for line in lines), stats.out
    assert any(line.startswith("review") and "other" in line for line in lines), stats.out


def test_a_braked_executor_holds_back_only_the_stages_that_run_on_it(factory: Harness) -> None:
    other = with_other(factory)
    factory.project["throttle"] = {"five_hour": {"cap": 0.5}}
    far = factory.clock.now() + timedelta(days=1)
    factory.executor.window_usage = five_hour(0.3, far)

    def spend(request: object) -> None:
        factory.executor.on_start = None
        factory.executor.window_usage = five_hour(0.6, far)

    def reset_soon(request: object) -> None:
        other.on_start = None
        factory.executor.window_usage = five_hour(0.6, factory.clock.now() + timedelta(seconds=600))

    factory.executor.on_start = spend
    # The brake holds until the review of t1 has started on the other Executor, however long the Run takes to get there.
    other.on_start = reset_soon
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
    [braked_at], [let_go_at] = factory.times(run_id, "ThrottleOn"), factory.times(run_id, "ThrottleOff")
    assert braked_at <= times[1] < let_go_at <= times[2]
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
    far, other_resets = now + timedelta(days=1), now + timedelta(seconds=300)
    factory.executor.window_usage = five_hour(0.3, far)
    other.window_usage = {"primary": WindowUsage(0.9, other_resets, WINDOW_SECONDS["five_hour"])}

    def spend(request: object) -> None:
        factory.executor.window_usage = five_hour(0.6, far)

    def reset_soon(request: object) -> None:
        factory.executor.window_usage = five_hour(0.6, factory.clock.now() + timedelta(seconds=600))

    factory.executor.on_start = spend
    # The brake of claude-code holds until the review has started, however long the Run takes to get there.
    other.on_start = reset_soon
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
    assert [e["window"] for e in factory.events(run_id, "ThrottleOff")] == ["other.primary", "claude-code.five_hour"]
    # The review on the other Executor waits for its window, though claude-code is still braked.
    review = factory.times(run_id, "AttemptStarted")[1]
    assert other_resets <= review < factory.times(run_id, "ThrottleOff")[1]
    [status] = seen
    assert "braked by claude-code.five_hour at 60%, above its cap 50%" in status.splitlines()[0]


def on_other_only(factory: Harness) -> OtherExecutor:
    """Has the project run on the Executor `other`, which reports `five_hour`, and on no other."""
    other = OtherExecutor()
    other.reported_windows = ("five_hour",)
    factory.executor = other
    factory.registries.executors.register("other", Kind(lambda settings, tools: other))
    factory.project["executor"] = "other"
    other.scripts["implement"] = HELLO
    return other


def test_a_bare_window_of_the_project_file_brakes_the_projects_executor(factory: Harness) -> None:
    other = on_other_only(factory)
    resets_at = factory.clock.now() + timedelta(seconds=300)
    other.window_usage = five_hour(0.9, resets_at)
    factory.project["throttle"] = {"five_hour": {"cap": 0.5}}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("one")})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [on] = factory.events(run_id, "ThrottleOn")
    assert (on["window"], on["limit"]) == ("other.five_hour", 0.5)
    assert factory.times(run_id, "AttemptStarted")[0] >= resets_at


def test_a_bare_window_of_a_limits_command_brakes_the_projects_executor(factory: Harness) -> None:
    other = on_other_only(factory)
    other.window_usage = five_hour(0.9, factory.clock.now() + timedelta(days=1))
    owner = Owner(factory).then(started(factory, "implement"), "limits", "--cap", "five_hour=0.5")
    owner.then(lambda run_id: bool(factory.events(run_id, "ThrottleOn")), "limits", "--no-throttle", "five_hour")
    release_when(factory, owner, 2)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("one"), "t2": ticket_text("two", ["t1"])})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [e["window"] for e in factory.events(run_id, "ThrottleOn")] == ["other.five_hour"]
    assert [e["throttle"] for e in factory.events(run_id, "LimitsChanged")] == [
        {"other.five_hour": {"cap": 0.5}},
        {"other.five_hour": None},
    ]


def pipeline_of(factory: Harness) -> Pipeline:
    machine = factory.machine()
    return load_project(factory.project_file, machine.library, machine.kinds).pipeline


def test_a_bare_window_of_an_old_limits_changed_is_the_window_of_the_projects_executor(factory: Harness) -> None:
    payload = {"max_parallel": None, "rounds": {}, "throttle": {"five_hour": {"cap": 0.5}}}
    event = EventRow(factory.clock.now(), Event.LimitsChanged, payload)
    pipeline = pipeline_of(factory)

    memory = replay(pipeline, [], [event], 1, executor="other")

    assert memory.throttle == {"other.five_hour": Brake(cap=0.5)}


def test_check_warns_of_a_brake_and_a_budget_on_an_executor_that_runs_no_node(factory: Harness) -> None:
    factory.registries.executors.register("other", Kind(lambda settings, tools: OtherExecutor()))
    factory.project["throttle"] = {
        "other.primary": {"cap": 0.5},
        "other.budget_usd": 5,
        "five_hour": {"cap": 0.5},
        "claude-code.budget_usd": 5,
        "budget_usd": 5,
    }

    result = factory.check()

    assert result.code == 0, result.err
    assert result.err.splitlines() == [
        "factory: warning: throttle.other.primary: no node of the Pipeline runs on the Executor other,"
        " so this brake holds nothing back",
        "factory: warning: throttle.other.budget_usd: no node of the Pipeline runs on the Executor other,"
        " so this budget holds nothing back",
    ]


@dataclass(frozen=True)
class Home:
    path: str


def parse_home(body: dict[str, Any], report: Report) -> Home:
    return Home(body.get("home", "/default"))


class HomedExecutor(OtherExecutor):
    """HomedExecutor is an Executor made with a `home`, and records the `home` each of its Attempts runs with."""

    def __init__(self, name: str, home: Home, scripts: dict[str, Any], ran: dict[str, Home]) -> None:
        super().__init__()
        self.name = name
        self.home = home
        self.scripts = scripts
        self.ran = ran

    def start(self, request: AttemptRequest, sandbox: Sandbox) -> ScriptedHandle:
        self.ran[request.stage] = self.home
        return super().start(request, sandbox)


def with_homes(factory: Harness) -> dict[str, Home]:
    """Has `implement` run on `first`, the project's Executor, and `review` on `second`, kinds that each take a `home`,
    and returns the `home` that each Stage ran with."""
    ran: dict[str, Home] = {}
    with_review(factory)
    scripts = factory.executor.scripts

    def kind(name: str) -> Kind[HomedExecutor]:
        return Kind(lambda home, tools: HomedExecutor(name, home, scripts, ran), ("home",), parse_home)

    factory.registries.executors.register("first", kind("first"))
    factory.registries.executors.register("second", kind("second"))
    factory.project["executor"] = "first"
    factory.pipeline["stages"]["review"]["executor"] = "second"
    return ran


def test_a_run_hands_each_executor_the_settings_of_its_block_and_set_overrides_one(factory: Harness) -> None:
    ran = with_homes(factory)
    factory.project["executors"] = {"second": {"home": "/second"}}
    factory.project["executor"] = {"kind": "first", "home": "/first"}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("one")})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert (ran["implement"], ran["review"]) == (Home("/first"), Home("/second"))

    overridden = factory.run(spec, "--set", "executors.second.home=/set")

    assert overridden.code == 0, overridden.out + overridden.err
    assert (ran["implement"], ran["review"]) == (Home("/first"), Home("/set"))
    run_id = run_id_of(overridden.out)
    [started] = factory.events(run_id, "RunStarted")
    assert started["settings"]["executors"] == {"second": {"home": "/set"}}
    assert started["settings_unlike_file"] == ["executors"]


def test_the_block_of_the_projects_kind_in_executors_lays_over_the_block_of_executor(factory: Harness) -> None:
    ran = with_homes(factory)
    factory.project["executor"] = {"kind": "first", "home": "/first"}
    factory.project["executors"] = {"first": {"home": "/more"}}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("one")})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert (ran["implement"], ran["review"]) == (Home("/more"), Home("/default"))


def test_factory_check_hands_each_executor_kind_the_settings_of_its_block(factory: Harness) -> None:
    def check(settings: Home, context: CheckContext) -> Problems:
        return Problems(warnings=(f"second runs in {settings.path}",))

    with_homes(factory)
    second = factory.registries.executors["second"]
    factory.registries.executors.register("second", Kind(second.make, second.fields, second.parse, check))
    factory.project["executors"] = {"second": {"home": "/second"}}

    assert factory.check().err.splitlines() == ["factory: warning: second runs in /second"]


def test_check_refuses_a_block_of_executors_it_cannot_read(factory: Harness) -> None:
    with_homes(factory)
    factory.project["executors"] = {"nobody": {"home": "/x"}, "first": "fast", "second": {"kind": "first"}}

    assert project_errors_of(factory) == [
        "factory.yaml: executors.first: must be a mapping of the settings of that kind",
        "factory.yaml: executors.nobody: names no Executor, must be one of claude-code, first, second",
        "factory.yaml: executors.second.kind: the key of the block names the kind",
    ]


def test_check_refuses_executors_that_is_no_mapping_and_a_field_no_kind_takes(factory: Harness) -> None:
    factory.project["executors"] = ["claude-code"]

    assert project_errors_of(factory) == ["factory.yaml: executors: must be a mapping"]

    with_homes(factory)
    factory.project["executors"] = {"second": {"color": "red"}}

    assert project_errors_of(factory) == ["factory.yaml: executors.second.color: unknown field"]
