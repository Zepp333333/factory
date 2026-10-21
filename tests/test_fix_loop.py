import json
import os
from pathlib import Path
from typing import Any

from factory.executor import AttemptRequest
from harness import AgentScript, Harness, run_id_of

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
NOTHING = AgentScript(output={"summary": "Look around", "handoff": "found nothing to do"})
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "wrote hello.txt"})


def fix_loop(factory: Harness, **fix: Any) -> None:
    """Sends a failed Gate `build` to a Stage `fix` that reads its cause and the latest code handoff."""
    factory.pipeline["stages"]["implement"]["next"]["done"] = "build"
    factory.pipeline["stages"]["fix"] = {
        "role": "implementer",
        "inputs": ["ticket", "cause", {"stage": ["implement", "fix"]}],
        "next": {"done": "build"},
        **fix,
    }
    factory.pipeline["gates"] = {
        "build": {"kind": "command", "with": {"run": "test -f hello.txt"}, "next": {"pass": "merge", "fail": "fix"}}
    }


def requests(factory: Harness, stage: str) -> list[AttemptRequest]:
    return [r for r in factory.executor.requests if r.stage == stage]


def brief(request: AttemptRequest) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(request.brief.read_text())
    return data


def nodes(factory: Harness, run_id: str) -> list[str]:
    return [e["node"] for e in factory.events(run_id, "NodeEntered")]


def test_fix_gets_only_its_declared_inputs_and_skips_an_optional_one_without_a_source(factory: Harness) -> None:
    fix_loop(factory)
    factory.pipeline["stages"]["fix"]["inputs"].append({"gate": "build", "optional": True, "rounds": "all"})
    factory.pipeline["stages"]["implement"]["inputs"].append({"gate": "build", "optional": True})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    run_dir = factory.run_dir(run_id)
    [implement] = requests(factory, "implement")
    assert brief(implement)["inputs"] == [
        {"source": "spec", "path": str(run_dir / "spec.md")},
        {"source": "ticket", "path": str(run_dir / "tickets" / "t1" / "ticket.md")},
    ]
    [fix] = requests(factory, "fix")
    implement_dir = implement.attempt_dir
    main = factory.git("rev-parse", "main")
    verdict = run_dir / "tickets" / "t1" / "build" / "probe-1" / "verdict.json"
    assert brief(fix)["inputs"] == [
        {"source": "ticket", "path": str(run_dir / "tickets" / "t1" / "ticket.md")},
        {"source": "cause", "kind": "gate", "node": "build", "probe": 1, "path": str(verdict)},
        {
            "source": "stage",
            "node": "implement",
            "round": 1,
            "attempt": 1,
            "path": str(implement_dir / "output.json"),
            "head_before": main,
            "head_after": main,
        },
        {"source": "gate", "node": "build", "probe": 1, "path": str(verdict)},
    ]
    assert brief(fix)["output_dir"] == str(fix.attempt_dir)


def test_inputs_are_read_only_artifacts_outside_the_workspace(factory: Harness) -> None:
    fix_loop(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = HELLO

    run_id = run_id_of(factory.run(spec).out)

    [fix] = requests(factory, "fix")
    workspace = factory.workspace(run_id, "t1")
    for item in brief(fix)["inputs"]:
        path = Path(item["path"])
        assert path.is_relative_to(factory.run_dir(run_id))
        assert not path.is_relative_to(workspace)
        assert not os.access(path, os.W_OK)
    assert json.loads((fix.attempt_dir / "output.json").read_text())["handoff"] == "wrote hello.txt"


def test_next_round_gets_the_handoff_of_the_last_one_and_its_commit_range(factory: Harness) -> None:
    fix_loop(factory)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    notes = AgentScript(files={"notes.txt": "plan\n"}, output={"handoff": "wrote notes, hello.txt is next"})
    factory.executor.scripts["fix"] = [notes, HELLO]

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert nodes(factory, run_id) == ["implement", "build", "fix", "build", "fix", "build", "merge", "done"]
    first, second = requests(factory, "fix")
    [handoff] = [i for i in brief(second)["inputs"] if i["source"] == "stage"]
    assert (handoff["node"], handoff["round"]) == ("fix", 1)
    assert json.loads(Path(handoff["path"]).read_text())["handoff"] == "wrote notes, hello.txt is next"
    changed = factory.git("diff", "--name-only", f"{handoff['head_before']}..{handoff['head_after']}")
    assert changed == "notes.txt"
    assert brief(second)["base"] == factory.git("rev-parse", "main")


def test_rounds_all_gives_every_round_of_the_named_stages(factory: Harness) -> None:
    fix_loop(factory)
    factory.pipeline["stages"]["fix"]["inputs"] = ["ticket", {"stage": ["implement", "fix"], "rounds": "all"}]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = [NOTHING, HELLO]

    factory.run(spec)

    _, second = requests(factory, "fix")
    rounds = [(i["node"], i["round"]) for i in brief(second)["inputs"] if i["source"] == "stage"]
    assert rounds == [("implement", 1), ("fix", 1)]


def test_fix_loop_runs_out_of_rounds_and_goes_to_then_with_exhausted(factory: Harness) -> None:
    fix_loop(factory, rounds={"max": 2, "then": "abandoned"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = NOTHING

    result = factory.run(spec)

    assert result.code == 1
    run_id = run_id_of(result.out)
    assert len(requests(factory, "fix")) == 2
    assert nodes(factory, run_id) == ["implement", "build", "fix", "build", "fix", "build", "abandoned"]
    [exhausted] = factory.events(run_id, "RoundsExhausted")
    assert exhausted == {"ticket": "t1", "node": "fix", "rounds": 2, "outcome": "exhausted", "to": "abandoned"}
    row = factory.cli("status", run_id).out.splitlines()[2]
    assert row.split() == ["t1", "abandoned", "fix", "2", "1", "abandoned"]


def test_stage_after_exhausted_gets_the_cause_that_sent_the_ticket_into_the_loop(factory: Harness) -> None:
    fix_loop(factory, rounds={"max": 1, "then": "rescue"})
    factory.pipeline["stages"]["rescue"] = {"role": "implementer", "inputs": ["cause"], "next": {"done": "merge"}}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = NOTHING
    factory.executor.scripts["rescue"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out
    [rescue] = requests(factory, "rescue")
    [cause] = brief(rescue)["inputs"]
    assert (cause["kind"], cause["node"], cause["probe"]) == ("gate", "build", 2)


def test_stage_switches_to_the_escalation_model_from_the_given_round(factory: Harness) -> None:
    fix_loop(factory, escalate={"from_round": 2, "model": "opus"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = [NOTHING, NOTHING, HELLO]

    run_id = run_id_of(factory.run(spec).out)

    assert [r.model for r in requests(factory, "fix")] == ["sonnet", "opus", "opus"]
    models = [e["model"] for e in factory.events(run_id, "AttemptStarted") if e["node"] == "fix"]
    assert models == ["sonnet", "opus", "opus"]


def test_route_with_reset_rounds_is_journaled(factory: Harness) -> None:
    fix_loop(factory, rounds={"max": 2, "then": "abandoned"})
    factory.pipeline["stages"]["implement"]["next"]["done"] = {"to": "build", "reset_rounds": ["fix"]}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = [NOTHING, HELLO]

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "RoundsReset") == [{"ticket": "t1", "node": "implement", "stages": ["fix"]}]
