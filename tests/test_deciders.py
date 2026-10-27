import json
from typing import Any

from harness import AgentScript, Harness, run_id_of
from test_check import errors_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})

REVIEWER_SCHEMA = {
    "type": "object",
    "required": ["door", "blast_radius"],
    "properties": {
        "door": {"type": "string"},
        "blast_radius": {"type": "string"},
        "danger_notes": {"type": "string"},
    },
}
RULER_SCHEMA = {
    "type": "object",
    "required": ["outcome"],
    "properties": {"outcome": {"type": "string"}, "reasons": {"type": "array", "items": {"type": "string"}}},
    "additionalProperties": False,
}


def review(door: str = "two_way", blast_radius: str = "local", **extra: str) -> AgentScript:
    return AgentScript(output={"door": door, "blast_radius": blast_radius, **extra})


def ruling(outcome: str, *reasons: str) -> AgentScript:
    return AgentScript(output={"outcome": outcome, "reasons": list(reasons)})


def deciding(factory: Harness, decider: dict[str, Any], outcomes: list[str] | None = None, **next: str) -> None:
    """Puts a Stage `review` and a Decision `judge` between `implement` and `sync`, and `ready` leads on to `sync`."""
    config = factory.config
    (config / "prompts" / "reviewer.md").write_text("Review the change.\n")
    (config / "schemas" / "reviewer.json").write_text(json.dumps(REVIEWER_SCHEMA))
    (config / "prompts" / "ruler.md").write_text("Rule on the change.\n")
    (config / "schemas" / "ruler.json").write_text(json.dumps(RULER_SCHEMA))
    factory.pipeline["roles"]["reviewer"] = {"prompt": "prompts/reviewer.md", "output": "schemas/reviewer.json"}
    factory.pipeline["roles"]["ruler"] = {"prompt": "prompts/ruler.md", "output": "schemas/ruler.json"}
    factory.pipeline["stages"]["implement"]["next"]["done"] = "review"
    factory.pipeline["stages"]["review"] = {
        "role": "reviewer",
        "workspace": "ro",
        "inputs": ["spec", "ticket"],
        "next": {"done": "judge"},
    }
    factory.pipeline["decisions"] = {
        "judge": {
            "decider": decider,
            "outcomes": outcomes or ["risky", "safe"],
            "inputs": [{"stage": "review"}],
            "next": {"safe": "sync", "risky": "abandoned", **next},
        }
    }
    factory.executor.scripts["implement"] = HELLO


RISK = {"kind": "function", "function": "risk"}


def decisions(factory: Harness, run_id: str) -> list[dict[str, Any]]:
    return factory.events(run_id, "DecisionDecided")


def decision_file(factory: Harness, run_id: str, round: int = 1) -> dict[str, Any]:
    path = factory.run_dir(run_id) / "tickets" / "t1" / "judge" / f"round-{round}" / "decision.json"
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def test_risk_function_lets_a_change_below_the_thresholds_on_and_writes_its_outcome_as_an_artifact(
    factory: Harness,
) -> None:
    deciding(factory, RISK)
    factory.executor.scripts["review"] = review("two_way", "module")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    assert decision_file(factory, run_id) == {
        "decision": "judge",
        "decider": "function",
        "outcome": "safe",
        "reasons": ["door two_way and blast_radius module are below the thresholds"],
    }
    [event] = decisions(factory, run_id)
    assert (event["outcome"], event["reasons"]) == ("safe", decision_file(factory, run_id)["reasons"])
    assert all(r.stage != "judge" for r in factory.executor.requests)


def test_risk_function_sends_a_one_way_door_on_its_risky_route_with_the_reviewers_notes(factory: Harness) -> None:
    deciding(factory, RISK)
    factory.executor.scripts["review"] = review("one_way", "local", danger_notes="drops a column")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    run_id = run_id_of(factory.run(spec).out)

    assert decision_file(factory, run_id) == {
        "decision": "judge",
        "decider": "function",
        "outcome": "risky",
        "reasons": ["door is one_way, and the threshold is one_way", "drops a column"],
    }
    assert factory.git("rev-parse", f"factory/{run_id}") == factory.git("rev-parse", "main")
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")][-2:] == ["judge", "abandoned"]


def test_risk_function_defaults_to_cross_module_and_the_project_file_sets_the_thresholds(factory: Harness) -> None:
    deciding(factory, RISK)
    factory.executor.scripts["review"] = review("two_way", "cross_module")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    default = run_id_of(factory.run(spec).out)
    factory.project["overrides"] = {"decisions": {"judge": {"decider": {"with": {"blast_radius": "external"}}}}}
    raised = run_id_of(factory.run(spec).out)

    [first] = decisions(factory, default)
    [second] = decisions(factory, raised)
    assert (first["outcome"], second["outcome"]) == ("risky", "safe")
    assert "blast_radius is cross_module, and the threshold is cross_module" in first["reasons"]


def test_function_outcomes_come_from_with_and_must_be_declared(factory: Harness) -> None:
    deciding(factory, {**RISK, "with": {"risky": "human", "safe": "ready"}}, ["human", "ready"], human="abandoned")
    factory.pipeline["decisions"]["judge"]["next"] = {"human": "abandoned", "ready": "sync"}
    factory.executor.scripts["review"] = review("one_way")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    run_id = run_id_of(factory.run(spec).out)

    assert decisions(factory, run_id)[0]["outcome"] == "human"


def test_risk_function_escalates_to_escalate_to_when_the_review_lacks_door_and_blast_radius(factory: Harness) -> None:
    deciding(factory, RISK)
    factory.executor.scripts["review"] = AgentScript(output={"door": "sideways", "blast_radius": "local"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    run_id = run_id_of(factory.run(spec).out)

    [event] = decisions(factory, run_id)
    assert event["outcome"] == "escalate"
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")][-2:] == ["judge", "abandoned"]


def test_an_escalate_route_in_next_wins_over_escalate_to(factory: Harness) -> None:
    deciding(factory, RISK, escalate="sync")
    factory.executor.scripts["review"] = AgentScript(output={"door": "sideways", "blast_radius": "local"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    run_id = run_id_of(factory.run(spec).out)

    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"


MODEL = {"kind": "model", "role": "ruler", "attempts": 2}


def test_model_decider_is_a_fresh_agent_that_gets_the_closed_list_and_picks_from_it(factory: Harness) -> None:
    deciding(factory, MODEL)
    factory.executor.scripts["review"] = review()
    factory.executor.scripts["judge"] = ruling("safe", "tests cover it")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    [request] = [r for r in factory.executor.requests if r.stage == "judge"]
    brief = json.loads(request.brief.read_text())
    assert brief["outcomes"] == ["risky", "safe", "escalate"]
    assert [i["source"] for i in brief["inputs"]] == ["stage"]
    assert decision_file(factory, run_id) == {
        "decision": "judge",
        "decider": "model",
        "outcome": "safe",
        "reasons": ["tests cover it"],
    }


def test_model_decider_with_an_outcome_off_the_list_is_tried_again(factory: Harness) -> None:
    deciding(factory, MODEL)
    factory.executor.scripts["review"] = review()
    factory.executor.scripts["judge"] = [ruling("ship it"), ruling("safe")]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    run_id = run_id_of(factory.run(spec).out)

    assert [r.stage for r in factory.executor.requests].count("judge") == 2
    assert decision_file(factory, run_id)["outcome"] == "safe"
    failed = [e for e in factory.events(run_id, "AttemptFinished") if e["node"] == "judge" and e["outcome"] == "failed"]
    assert "ship it" in failed[0]["errors"][0]


def test_model_decider_that_keeps_answering_off_the_list_escalates(factory: Harness) -> None:
    deciding(factory, MODEL)
    factory.executor.scripts["review"] = review()
    factory.executor.scripts["judge"] = ruling("ship it")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    run_id = run_id_of(factory.run(spec).out)

    assert [r.stage for r in factory.executor.requests].count("judge") == 2
    assert decision_file(factory, run_id)["outcome"] == "escalate"
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")][-2:] == ["judge", "abandoned"]


def test_model_decider_that_crashes_is_replaced_and_then_escalates(factory: Harness) -> None:
    deciding(factory, MODEL)
    factory.executor.scripts["review"] = review()
    crash = AgentScript(fail="crash")
    factory.executor.scripts["judge"] = [crash, ruling("safe")]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    first = run_id_of(factory.run(spec).out)

    assert {e["outcome"] for e in decisions(factory, first)} == {"safe"}
    assert [e["cause"] for e in factory.events(first, "AttemptFinished") if e["outcome"] == "failed"] == [
        "executor_crash"
    ]

    factory.executor.scripts["judge"] = crash
    second = run_id_of(factory.run(spec).out)

    assert {e["outcome"] for e in decisions(factory, second)} == {"escalate"}


def test_model_decider_changes_to_the_workspace_are_discarded(factory: Harness) -> None:
    deciding(factory, MODEL)
    factory.executor.scripts["review"] = review()
    factory.executor.scripts["judge"] = AgentScript(
        files={"sneaky.txt": "x\n"}, output={"outcome": "safe", "reasons": []}
    )
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    run_id = run_id_of(factory.run(spec).out)

    assert factory.events(run_id, "WorkspaceViolated")[0]["node"] == "judge"
    assert factory.git("ls-tree", "--name-only", f"factory/{run_id}").split() == [
        "README.md",
        "factory.yaml",
        "hello.txt",
    ]


def test_check_reports_a_function_decider_that_cannot_run(factory: Harness) -> None:
    deciding(factory, {"kind": "function", "function": "risk", "with": {"risky": "stop", "door": "ajar"}})
    factory.pipeline["decisions"]["judge"]["decider"]["role"] = "ruler"
    factory.pipeline["decisions"]["other"] = {
        "decider": {"kind": "function", "function": "luck"},
        "outcomes": ["a"],
        "next": {"a": "done"},
    }
    factory.pipeline["decisions"]["third"] = {
        "decider": {"kind": "model"},
        "outcomes": ["a"],
        "next": {"a": "done"},
    }

    errors = errors_of(factory)

    assert "decisions.judge.decider.with.door: must be one of two_way, one_way" in errors
    assert "decisions.other.decider: unknown function luck" in errors
    assert "decisions.third.decider: a model Decider needs a role" in errors


def test_check_requires_the_outcomes_of_a_risk_function_to_be_declared(factory: Harness) -> None:
    deciding(factory, RISK, ["safe"])

    assert "decisions.judge.decider.with: outcome risky is not declared in outcomes" in errors_of(factory)
