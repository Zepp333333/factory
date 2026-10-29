import pytest

from harness import AgentScript, Harness, ticket_text, writes
from test_describer import DESCRIPTION, with_describer
from test_recovery import attempt_started, crashed_run, killed_runs_run_no_handler

pytestmark = pytest.mark.usefixtures(killed_runs_run_no_handler.__name__)


def status_lines(factory: Harness, run_id: str) -> list[str]:
    result = factory.cli("status", run_id)
    assert result.code == 0, result.out + result.err
    return result.out.splitlines()


def test_status_says_a_queued_ticket_waits_for_its_unmerged_blockers(factory: Harness) -> None:
    factory.project["max_parallel"] = 3
    tickets = {"a": ticket_text("a"), "b": ticket_text("b", ["a"]), "c": ticket_text("c", ["b"])}
    spec = factory.write_spec("Chain", "A chain.", tickets)
    factory.executor.scripts["implement"] = AgentScript(wait_for=factory.root / "never")
    run_id = crashed_run(factory, spec, attempt_started(factory))

    lines = status_lines(factory, run_id)

    assert "Ticket b waits for a" in lines
    assert "Ticket c waits for b" in lines
    assert not any(line.startswith("Ticket a ") for line in lines)


def test_status_says_a_ticket_with_merged_blockers_waits_for_a_slot(factory: Harness) -> None:
    factory.project["max_parallel"] = 1
    spec = factory.write_spec("Pair", "Two.", {"a": ticket_text("a"), "b": ticket_text("b")})
    factory.executor.scripts["implement"] = AgentScript(wait_for=factory.root / "never")
    run_id = crashed_run(factory, spec, attempt_started(factory))

    lines = status_lines(factory, run_id)

    assert "Ticket b waits for a slot" in lines


def test_status_names_the_abandoned_blocker_of_a_ticket_that_never_starts(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    tickets = {"a": ticket_text("a"), "b": ticket_text("b", ["a"]), "c": ticket_text("c", ["b"])}
    spec = factory.write_spec("Chain", "A chain.", tickets)
    factory.executor.ticket_scripts[("a", "implement")] = AgentScript(output={"summary": "no handoff"})

    result = factory.run(spec)

    assert result.code == 1
    run_id = factory.run_ids()[-1]
    lines = status_lines(factory, run_id)
    assert "Ticket b never starts, blocked by abandoned a" in lines
    assert "Ticket c never starts, blocked by abandoned a" in lines


def test_status_shows_the_describe_phase_of_a_run_that_writes_the_pr(factory: Harness) -> None:
    spec = factory.write_spec("One", "One.", {"a": ticket_text("a")})
    with_describer(factory)
    factory.executor.scripts["implement"] = writes("a")
    factory.executor.scripts["describe"] = AgentScript(output=DESCRIPTION.output, wait_for=factory.root / "never")
    run_id = crashed_run(factory, spec, lambda run_id: bool(factory.events(run_id, "DescriberStarted")))

    lines = status_lines(factory, run_id)

    assert "Run phase: describe" in lines
