import json
from typing import Any

from factory.pipeline import deciders
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


def ruler_schema(outcomes: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "required": ["outcome"],
        "properties": {"outcome": {"enum": outcomes}, "reasons": {"type": "array", "items": {"type": "string"}}},
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
    (config / "schemas" / "ruler.json").write_text(json.dumps(ruler_schema(outcomes or ["risky", "safe"])))
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


def test_risk_function_decides_with_the_thresholds_of_the_project_file_and_writes_its_outcome_as_an_artifact(
    factory: Harness,
) -> None:
    deciding(factory, RISK)
    factory.project["overrides"] = {"decisions": {"judge": {"decider": {"with": {"blast_radius": "external"}}}}}
    factory.executor.scripts["review"] = review("two_way", "cross_module")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    assert decision_file(factory, run_id) == {
        "decision": "judge",
        "decider": "function",
        "outcome": "safe",
        "reasons": ["door two_way and blast_radius cross_module are below the thresholds"],
    }
    [event] = decisions(factory, run_id)
    assert (event["outcome"], event["reasons"]) == ("safe", decision_file(factory, run_id)["reasons"])
    assert all(r.stage != "judge" for r in factory.executor.requests)


def test_risk_function_defaults_to_one_way_and_cross_module_and_sends_a_change_that_reaches_one_on_risky() -> None:
    with_, errors = deciders.resolve_with("risk", {})

    assert errors == []
    assert deciders.decide("risk", with_, [{"door": "two_way", "blast_radius": "module"}]) == deciders.Ruling(
        "safe", ["door two_way and blast_radius module are below the thresholds"]
    )
    assert deciders.decide("risk", with_, [{"door": "two_way", "blast_radius": "cross_module"}]) == deciders.Ruling(
        "risky", ["blast_radius is cross_module, and the threshold is cross_module"]
    )


def test_risk_function_gives_a_one_way_door_its_risky_outcome_with_the_reviewers_notes() -> None:
    with_, _ = deciders.resolve_with("risk", {})
    review = {"door": "one_way", "blast_radius": "local", "danger_notes": "drops a column"}

    assert deciders.decide("risk", with_, [review]) == deciders.Ruling(
        "risky", ["door is one_way, and the threshold is one_way", "drops a column"]
    )


def test_risk_function_takes_its_outcome_names_from_with() -> None:
    with_, _ = deciders.resolve_with("risk", {"risky": "human", "safe": "ready"})

    assert deciders.outcomes_of("risk", with_) == ["human", "ready"]
    assert deciders.decide("risk", with_, [{"door": "one_way", "blast_radius": "local"}]).outcome == "human"


def test_risk_function_escalates_when_no_output_carries_a_valid_door_and_blast_radius() -> None:
    with_, _ = deciders.resolve_with("risk", {})

    assert deciders.decide("risk", with_, [{"door": "sideways", "blast_radius": "local"}]) == deciders.Ruling(
        "escalate", ["no Stage output carries a valid door and blast_radius"]
    )


def test_an_escalate_route_in_next_wins_over_escalate_to(factory: Harness) -> None:
    deciding(factory, RISK, escalate="sync")
    factory.executor.scripts["review"] = AgentScript(output={"door": "sideways", "blast_radius": "local"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    run_id = run_id_of(factory.run(spec).out)

    assert [e["outcome"] for e in decisions(factory, run_id)] == ["escalate"]
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"


MODEL = {"kind": "model", "role": "ruler", "attempts": 2}


def test_model_decider_is_a_fresh_agent_that_is_tried_again_off_the_closed_list_and_loses_its_workspace_changes(
    factory: Harness,
) -> None:
    deciding(factory, MODEL)
    factory.executor.scripts["review"] = review()
    sneaky = AgentScript(files={"sneaky.txt": "x\n"}, output={"outcome": "safe", "reasons": ["tests cover it"]})
    factory.executor.scripts["judge"] = [ruling("ship it"), sneaky]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    first, _ = [r for r in factory.executor.requests if r.stage == "judge"]
    brief = json.loads(first.brief.read_text())
    assert brief["outcomes"] == ["risky", "safe", "escalate"]
    assert [i["source"] for i in brief["inputs"]] == ["stage"]
    [failed] = [
        e for e in factory.events(run_id, "AttemptFinished") if e["node"] == "judge" and e["outcome"] == "failed"
    ]
    assert "ship it" in failed["errors"][0]
    assert decision_file(factory, run_id) == {
        "decision": "judge",
        "decider": "model",
        "outcome": "safe",
        "reasons": ["tests cover it"],
    }
    assert factory.events(run_id, "WorkspaceViolated")[0]["node"] == "judge"
    assert factory.git("ls-tree", "--name-only", f"factory/{run_id}").split() == [
        "README.md",
        "factory.yaml",
        "hello.txt",
    ]


def test_model_decider_that_crashes_is_replaced_and_escalates_once_every_attempt_failed(factory: Harness) -> None:
    deciding(factory, MODEL)
    factory.executor.scripts["review"] = review()
    factory.executor.scripts["judge"] = [AgentScript(fail="crash"), ruling("ship it")]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    run_id = run_id_of(factory.run(spec).out)

    judged = [e for e in factory.events(run_id, "AttemptFinished") if e["node"] == "judge"]
    assert [e["outcome"] for e in judged] == ["failed", "failed"]
    assert "the agent crashed" in judged[0]["reason"]
    assert "ship it" in judged[1]["errors"][0]
    assert decision_file(factory, run_id) == {
        "decision": "judge",
        "decider": "model",
        "outcome": "escalate",
        "reasons": ["the model Decider failed all its Attempts"],
    }
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")][-2:] == ["judge", "abandoned"]


def test_model_decider_whose_sandbox_is_lost_spends_no_attempt_and_escalates_past_sandbox_retries(
    factory: Harness,
) -> None:
    deciding(factory, {**MODEL, "attempts": 1})
    factory.pipeline["defaults"]["sandbox_retries"] = 1
    factory.executor.scripts["review"] = review()
    factory.executor.scripts["judge"] = ruling("safe")
    factory.sandbox.refuse_node = "judge"
    factory.sandbox.refuse = 2
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    run_id = run_id_of(factory.run(spec).out)

    judged = [e for e in factory.events(run_id, "AttemptFinished") if e["node"] == "judge"]
    assert [e.get("cause") for e in judged] == ["sandbox_start", "sandbox_start"]
    assert decision_file(factory, run_id) == {
        "decision": "judge",
        "decider": "model",
        "outcome": "escalate",
        "reasons": ["the model Decider failed all its Attempts"],
    }
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")][-2:] == ["judge", "abandoned"]


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
