from harness import Harness, run_id_of, ticket_text, writes


def checked_run(factory: Harness, tickets: list[str], **gate: object) -> str:
    """Runs Tickets through `check -> catch_up -> check_combined`, where both Gates append a line to `probes.log`."""
    factory.project["max_parallel"] = len(tickets)
    command = {"run": f"echo probe >> {factory.root}/probes.log"}
    body = {"kind": "command", "with": command, **gate}
    factory.pipeline["stages"]["implement"]["next"]["done"] = "check"
    factory.pipeline["gates"] = {
        "check": {**body, "next": {"pass": "catch_up", "fail": "abandoned"}},
        "check_combined": {**body, "next": {"pass": "merge", "fail": "abandoned"}},
    }
    factory.pipeline["operations"]["catch_up"]["next"]["clean"] = "check_combined"
    spec = factory.write_spec("Notes", "Notes.", {name: ticket_text(name) for name in tickets})
    for name in tickets:
        factory.executor.ticket_scripts[(name, "implement")] = writes(name)
    result = factory.run(spec)
    assert result.code == 0, result.out
    return run_id_of(result.out)


def probes_run(factory: Harness) -> int:
    return len((factory.root / "probes.log").read_text().splitlines())


def test_gate_after_a_catch_up_that_brought_nothing_passes_without_its_command_and_names_the_probe(
    factory: Harness,
) -> None:
    run_id = checked_run(factory, ["a"])

    assert probes_run(factory) == 1
    reused = next(event for event in factory.events(run_id, "GateFinished") if event["node"] == "check_combined")
    assert reused["outcome"] == "pass"
    assert reused["reused"] == {"gate": "check", "probe": 1}
    assert reused["artifact"]["node"] == "check_combined"
    timeline = factory.cli("status", run_id, "--timeline").out
    assert "check_combined pass reused check probe 1" in " ".join(timeline.split())


def test_gate_runs_again_when_another_ticket_merged_first(factory: Harness) -> None:
    run_id = checked_run(factory, ["a", "b"])

    assert probes_run(factory) == 3
    reused = [event["ticket"] for event in factory.events(run_id, "GateFinished") if "reused" in event]
    probed = [event["ticket"] for event in factory.events(run_id, "ProbeStarted") if event["node"] == "check_combined"]
    assert len(reused) == 1
    assert len(probed) == 1
    assert reused != probed


def test_gate_with_reuse_false_always_runs(factory: Harness) -> None:
    run_id = checked_run(factory, ["a"], reuse=False)

    assert probes_run(factory) == 2
    assert not [event for event in factory.events(run_id, "GateFinished") if "reused" in event]
