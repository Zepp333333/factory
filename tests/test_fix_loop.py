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


def test_fix_gets_only_its_declared_inputs_as_read_only_artifacts_and_rounds_all_gives_every_round(
    factory: Harness,
) -> None:
    fix_loop(factory, rounds={"max": 2, "then": "abandoned"})
    factory.pipeline["stages"]["fix"]["inputs"] = [
        "ticket",
        "cause",
        {"stage": ["implement", "fix"], "rounds": "all"},
        {"gate": "build", "optional": True, "rounds": "all"},
    ]
    factory.pipeline["stages"]["implement"]["inputs"].append({"gate": "build", "optional": True})
    factory.pipeline["stages"]["implement"]["next"]["done"] = {"to": "build", "reset_rounds": ["fix"]}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = [NOTHING, HELLO]

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    run_dir = factory.run_dir(run_id)

    # An optional input without a source is skipped.
    [implement] = requests(factory, "implement")
    assert brief(implement)["inputs"] == [
        {"source": "spec", "path": str(run_dir / "spec.md")},
        {"source": "ticket", "path": str(run_dir / "tickets" / "t1" / "ticket.md")},
    ]

    first, second = requests(factory, "fix")
    main = factory.git("rev-parse", "main")
    ticket = {"source": "ticket", "path": str(run_dir / "tickets" / "t1" / "ticket.md")}
    verdict = run_dir / "tickets" / "t1" / "build" / "probe-1" / "verdict.json"
    implemented = {
        "source": "stage",
        "node": "implement",
        "round": 1,
        "attempt": 1,
        "path": str(implement.attempt_dir / "output.json"),
        "head_before": main,
        "head_after": main,
    }
    assert brief(first)["inputs"] == [
        ticket,
        {"source": "cause", "kind": "gate", "node": "build", "probe": 1, "path": str(verdict)},
        implemented,
        {"source": "gate", "node": "build", "probe": 1, "path": str(verdict)},
    ]
    assert brief(first)["output_dir"] == str(first.attempt_dir)

    # `rounds: all` gives every Round of the named Stages and every Probe of the Gate so far.
    stages = [(i["node"], i["round"]) for i in brief(second)["inputs"] if i["source"] == "stage"]
    assert stages == [("implement", 1), ("fix", 1)]
    probes = [i["probe"] for i in brief(second)["inputs"] if i["source"] == "gate"]
    assert probes == [1, 2]

    # Inputs are read-only Artifacts outside the Workspace.
    workspace = factory.workspace(run_id, "t1")
    for item in brief(second)["inputs"]:
        path = Path(item["path"])
        assert path.is_relative_to(run_dir)
        assert not path.is_relative_to(workspace)
        assert not os.access(path, os.W_OK)
    assert json.loads((second.attempt_dir / "output.json").read_text())["handoff"] == "wrote hello.txt"

    assert factory.events(run_id, "RoundsReset") == [{"ticket": "t1", "node": "implement", "stages": ["fix"]}]


def test_next_round_gets_the_handoff_of_the_last_one_and_switches_to_the_escalation_model_from_its_round(
    factory: Harness,
) -> None:
    fix_loop(factory, escalate={"from_round": 2, "model": "opus"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    notes = AgentScript(files={"notes.txt": "plan\n"}, output={"handoff": "wrote notes, hello.txt is next"})
    factory.executor.scripts["fix"] = [notes, NOTHING, HELLO]

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert nodes(factory, run_id) == [
        *("implement", "build", "fix", "build", "fix", "build", "fix", "build"),
        *("merge", "done"),
    ]
    _, second, _ = requests(factory, "fix")
    [handoff] = [i for i in brief(second)["inputs"] if i["source"] == "stage"]
    assert (handoff["node"], handoff["round"]) == ("fix", 1)
    assert json.loads(Path(handoff["path"]).read_text())["handoff"] == "wrote notes, hello.txt is next"
    changed = factory.git("diff", "--name-only", f"{handoff['head_before']}..{handoff['head_after']}")
    assert changed == "notes.txt"
    assert brief(second)["base"] == factory.git("rev-parse", "main")
    assert [r.model for r in requests(factory, "fix")] == ["sonnet", "opus", "opus"]
    models = [e["model"] for e in factory.events(run_id, "AttemptStarted") if e["node"] == "fix"]
    assert models == ["sonnet", "opus", "opus"]
    row = factory.cli("status", run_id).out.splitlines()[2]
    assert row.split()[:7] == ["t1", "done", "fix", "3", "1", "0", "merged"]


def test_fix_loop_runs_out_of_rounds_and_its_then_stage_gets_the_cause_that_sent_the_ticket_into_it(
    factory: Harness,
) -> None:
    fix_loop(factory, rounds={"max": 1, "then": "rescue"})
    factory.pipeline["stages"]["rescue"] = {"role": "implementer", "inputs": ["cause"], "next": {"done": "merge"}}
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = NOTHING
    factory.executor.scripts["rescue"] = HELLO

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert len(requests(factory, "fix")) == 1
    assert nodes(factory, run_id) == ["implement", "build", "fix", "build", "rescue", "merge", "done"]
    [exhausted] = factory.events(run_id, "RoundsExhausted")
    assert exhausted == {"ticket": "t1", "node": "fix", "rounds": 1, "outcome": "exhausted", "to": "rescue"}
    [rescue] = requests(factory, "rescue")
    [cause] = brief(rescue)["inputs"]
    assert (cause["kind"], cause["node"], cause["probe"]) == ("gate", "build", 2)
