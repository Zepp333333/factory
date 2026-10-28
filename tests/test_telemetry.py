"""Run telemetry: the variant fingerprint, the metrics of Attempts, Tickets and Runs, and `factory stats`."""

import json
import re
from dataclasses import replace
from datetime import timedelta
from typing import Any

from factory.fingerprint import LENGTH, short_hash
from harness import RESETS_AT, AgentScript, Harness, run_id_of, ticket_text, writes
from test_fix_loop import HELLO, NOTHING, fix_loop

TICKET = ticket_text("Greet")
DONE = AgentScript(
    files={"hello.txt": "hello\n"},
    output={"summary": "Add hello.txt", "handoff": "done"},
    turns=7,
    cost=0.5,
    tokens=(10, 20, 30, 40),
)
HEX = re.compile(rf"[0-9a-f]{{{LENGTH}}}")


def stats(factory: Harness, *argv: str) -> Any:
    result = factory.cli("stats", *argv, "--json")
    assert result.code == 0, result.err
    return json.loads(result.out)


def test_attempt_started_writes_the_fingerprint_and_run_started_the_runs(factory: Harness) -> None:
    factory.executor.scripts["implement"] = DONE
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [run] = factory.events(run_id, "RunStarted")
    [attempt] = factory.events(run_id, "AttemptStarted")
    for name in ("pipeline_sha", "kit_sha"):
        assert HEX.fullmatch(run[name]), name
        assert attempt[name] == run[name]
    assert re.fullmatch(r"\d+\.\d+\.\d+(\+[0-9a-f]{7})?", run["factory_version"])
    assert attempt["factory_version"] == run["factory_version"]
    prompt = factory.config / "prompts" / "implementer.md"
    schema = factory.config / "schemas" / "implementer.json"
    assert attempt["prompt_sha"] == short_hash(prompt.read_bytes())
    assert attempt["schema_sha"] == short_hash(schema.read_bytes())
    assert attempt["model"] == "sonnet"


def test_the_fingerprint_follows_the_prompt_and_the_pipeline_files_and_not_the_run(factory: Harness) -> None:
    factory.executor.scripts["implement"] = DONE
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    first = factory.run(spec)
    factory.clock.sleep(1)
    second = factory.run(spec)
    (factory.config / "prompts" / "implementer.md").write_text("Implement the Ticket, briefly.\n")
    factory.clock.sleep(1)
    third = factory.run(spec)

    runs = [run_id_of(r.out) for r in (first, second, third)]
    one, two, three = ([*factory.events(r, "RunStarted"), *factory.events(r, "AttemptStarted")] for r in runs)

    assert one[0]["pipeline_sha"] == two[0]["pipeline_sha"] != three[0]["pipeline_sha"]
    assert one[1]["prompt_sha"] == two[1]["prompt_sha"] != three[1]["prompt_sha"]
    assert one[1]["schema_sha"] == three[1]["schema_sha"]
    assert one[0]["kit_sha"] == three[0]["kit_sha"]


def test_model_in_the_fingerprint_is_the_model_after_escalate(factory: Harness) -> None:
    fix_loop(factory, escalate={"from_round": 2, "model": "opus"}, rounds={"max": 3, "then": "abandoned"})
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = [NOTHING, HELLO]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    models = [(e["node"], e["round"], e["model"]) for e in factory.events(run_id, "AttemptStarted")]
    assert models == [("implement", 1, "sonnet"), ("fix", 1, "sonnet"), ("fix", 2, "opus")]


def test_attempt_finished_writes_the_stats_and_leaves_unknown_what_the_stream_lacks(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["attempts"] = 2
    factory.executor.scripts["implement"] = [AgentScript(fail="crash"), DONE]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    crashed, done = factory.events(run_id, "AttemptFinished")
    assert crashed["stats"]["api_requests"] is None
    assert done["stats"]["turns"] == 7
    assert done["stats"]["api_requests"] == 1
    assert done["stats"]["tokens"]["claude-main"] == {
        "input_tokens": 10,
        "output_tokens": 20,
        "thinking_tokens": None,
        "cache_read_tokens": 30,
        "cache_write_tokens": 40,
    }
    assert done["stats"]["permission_denials"] is None
    assert done["stats"]["utilization_start"] is None


def test_change_size_is_written_at_the_stage_boundary_and_at_the_merge(factory: Harness) -> None:
    factory.executor.scripts["implement"] = DONE
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    [boundary] = factory.events(run_id, "StageBoundaryCommitted")
    assert (boundary["files_changed"], boundary["lines_added"], boundary["lines_removed"]) == (1, 1, 0)
    [merged] = [e for e in factory.events(run_id, "OperationFinished") if e["outcome"] == "merged"]
    assert (merged["files_changed"], merged["lines_added"], merged["lines_removed"]) == (1, 1, 0)


def two_variants(factory: Harness) -> tuple[str, str]:
    """Runs one Ticket under two prompts, the second costing more, and returns the Run ids."""
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    runs = []
    for prompt, cost in (("Implement the Ticket.\n", 0.2), ("Implement the Ticket, briefly.\n", 0.6)):
        (factory.config / "prompts" / "implementer.md").write_text(prompt)
        factory.executor.scripts["implement"] = replace(DONE, cost=cost)
        factory.clock.sleep(1)
        result = factory.run(spec)
        assert result.code == 0, result.out
        runs.append(run_id_of(result.out))
    return runs[0], runs[1]


def test_stats_by_prompt_groups_attempts_with_n_median_and_p90(factory: Harness) -> None:
    first, second = two_variants(factory)

    data = stats(factory, first, second, "--by", "prompt")

    assert data["by"] == "prompt"
    rows = {row["prompt"]: row for row in data["stages"]}
    assert len(rows) == 2
    cheap, dear = (rows[sha] for sha in sorted(rows, key=lambda sha: rows[sha]["cost_usd_p50"]))
    assert (cheap["stage"], cheap["n_attempts"]) == ("implement", 1)
    assert (cheap["cost_usd_p50"], cheap["cost_usd_p90"]) == (0.2, 0.2)
    assert (dear["n_attempts"], dear["cost_usd_p50"]) == (1, 0.6)
    assert dear["api_requests_p50"] == 1
    assert dear["rounds_p50"] == 1
    assert dear["first_pass_pct"] == 100
    assert cheap["cache_hit_pct"] == 100 * 33 / (11 + 33 + 44)


def test_stats_pools_attempts_of_the_same_variant_into_percentiles(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    runs = []
    for cost in (0.1, 0.2, 0.3, 0.4, 0.5):
        factory.executor.scripts["implement"] = replace(DONE, cost=cost)
        factory.clock.sleep(1)
        runs.append(run_id_of(factory.run(spec).out))

    [row] = stats(factory, *runs, "--by", "model")["stages"]

    assert row["model"] == "sonnet"
    assert row["n_attempts"] == 5
    assert row["cost_usd_p50"] == 0.3
    assert abs(row["cost_usd_p90"] - 0.46) < 1e-9


def test_stats_text_prints_the_runs_and_their_stages_and_by_adds_the_column(factory: Harness) -> None:
    first, second = two_variants(factory)

    plain = factory.cli("stats", first, second)
    by = factory.cli("stats", first, second, "--by", "prompt", "--stage", "implement")

    assert plain.code == 0, plain.err
    assert f"Run {first}  done" in plain.out
    assert f"Run {second}  done" in plain.out
    assert "stage" in plain.out and "cost $ p50/p90" in plain.out and "first-pass %" in plain.out
    header, *rows = by.out.splitlines()
    assert header.split()[:2] == ["stage", "prompt"]
    assert len(rows) == 2 and all(row.startswith("implement") for row in rows)
    assert factory.cli("stats", first, "--by", "prompt", "--stage", "nothing").out.splitlines()[0].startswith("stage")


def test_stats_json_without_by_has_the_run_its_stages_tickets_and_attempts(factory: Harness) -> None:
    first, _ = two_variants(factory)

    [run] = stats(factory, first)["runs"]

    assert run["run"] == first and run["state"] == "done"
    assert run["cost_usd"] == 0.2
    assert run["tickets_merged"] == 1 and run["first_pass_pct"] == 100
    assert [s["stage"] for s in run["stages"]] == ["implement"]
    [ticket] = run["tickets"]
    assert ticket["ticket"] == "t1" and ticket["outcome"] == "merged" and ticket["by_hand"] is False
    assert ticket["merge_change"]["files"] == 1
    [attempt] = run["attempts"]
    assert attempt["model"] == "sonnet" and HEX.fullmatch(attempt["prompt_sha"])


def test_stats_of_an_unknown_run_is_an_error(factory: Harness) -> None:
    factory.executor.scripts["implement"] = DONE
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    assert factory.run(spec).code == 0

    result = factory.cli("stats", "nope")

    assert result.code == 2
    assert "no Run nope" in result.err


def test_gate_failures_and_convergence_are_counted(factory: Harness) -> None:
    fix_loop(factory)
    factory.executor.scripts["implement"] = NOTHING
    factory.executor.scripts["fix"] = HELLO
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    [run] = stats(factory, run_id)["runs"]
    [ticket] = run["tickets"]
    assert ticket["first_pass"] is False
    assert ticket["fix_rounds"] == 1
    assert [(g["node"], g["outcome"]) for g in ticket["gates"]] == [("build", "fail"), ("build", "pass")]
    assert ticket["attempts"] == {"implement": 1, "fix": 1}
    assert ticket["rounds"] == {"implement": 1, "fix": 1}
    assert run["first_pass_pct"] == 0
    assert all(isinstance(g["probe_duration_s"], float) for g in ticket["gates"])


def test_waiting_for_blockers_and_a_slot_is_counted_apart_from_work(factory: Harness) -> None:
    gate = factory.root / "go"
    factory.executor.scripts["implement"] = AgentScript(output={"handoff": "done"}, cost=0.1)
    factory.executor.ticket_scripts[("t1", "implement")] = AgentScript(output={"handoff": "done"}, wait_for=gate)

    def ninety_seconds_pass(seconds: float) -> None:
        if not gate.exists():
            factory.clock._now += timedelta(seconds=90)
            gate.touch()

    factory.clock.on_sleep = ninety_seconds_pass
    spec = factory.write_spec(
        "Greeting",
        "Say hello.",
        {"t1": TICKET, "t2": ticket_text("Second", ["t1"]), "t3": ticket_text("Third")},
    )

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [run] = stats(factory, run_id)["runs"]
    by_ticket = {t["ticket"]: t for t in run["tickets"]}
    assert by_ticket["t1"]["wait_blockers_s"] == 0
    assert by_ticket["t2"]["wait_blockers_s"] >= 90
    assert by_ticket["t3"]["wait_slot_s"] >= 90
    assert by_ticket["t3"]["wait_blockers_s"] == 0
    assert all(t["wait_pause_s"] == 0 for t in by_ticket.values())
    assert run["slots_full_pct"] > 0


def test_a_rate_limit_pause_is_counted_as_waiting_for_the_pause_and_not_for_a_slot(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = [AgentScript(fail="rate_limited"), DONE]

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [run] = stats(factory, run_id)["runs"]
    [ticket] = run["tickets"]
    [limited] = factory.times(run_id, "AttemptFinished")[:1]
    assert ticket["wait_pause_s"] == (RESETS_AT - limited).total_seconds()
    assert ticket["wait_slot_s"] == 0
    assert ticket["failures"] == {"rate_limited": 1}
    assert run["wait_pause_s"] == ticket["wait_pause_s"]


def test_waiting_for_the_merge_window_is_counted_per_ticket(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    queued = factory.root / "both-queued"
    factory.pipeline["operations"]["sync"]["next"]["clean"] = "check"
    factory.pipeline["gates"] = {
        "check": {
            "kind": "command",
            "with": {"run": f"until [ -e {queued} ]; do sleep 0.01; done"},
            "next": {"pass": "merge", "fail": "abandoned"},
        }
    }
    spec = factory.write_spec("Files", "Two files.", {name: ticket_text(name) for name in "ab"})
    for name in "ab":
        factory.executor.ticket_scripts[(name, "implement")] = writes(name)

    def open_once_both_queued(seconds: float) -> None:
        if sum(type == "MergeWindowQueued" for _, type, _ in factory.all_runs_events()) == 2:
            queued.touch()

    factory.clock.on_sleep = open_once_both_queued

    result = factory.run(spec)

    assert result.code == 0, result.out
    [run] = stats(factory, run_id_of(result.out))["runs"]
    waits = sorted(t["wait_merge_window_s"] for t in run["tickets"])
    assert waits[0] == 0 and waits[1] > 0
    assert run["wait_merge_window_s"] == waits[1]


def test_a_ticket_finished_by_hand_and_a_workspace_violation_show_in_its_metrics(factory: Harness) -> None:
    factory.pipeline["stages"]["review"] = {
        "role": "implementer",
        "workspace": "ro",
        "inputs": ["ticket"],
        "next": {"done": "sync"},
    }
    factory.pipeline["stages"]["implement"]["next"]["done"] = "review"
    factory.executor.scripts["implement"] = DONE
    factory.executor.scripts["review"] = AgentScript(files={"sneaky.txt": "x\n"}, output={"handoff": "looked"})
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    [run] = stats(factory, run_id_of(result.out))["runs"]
    [ticket] = run["tickets"]
    assert ticket["workspace_violations"] == 1
    assert ticket["by_hand"] is False
