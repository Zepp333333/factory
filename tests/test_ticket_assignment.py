"""A Run assigns the Tickets it takes to the account its Tracker works as, through the TicketBoard."""

from typing import Any

from factory.ports.kind import Kind
from factory.ports.tracker import SpecLink, TicketSnapshot, Tracker
from fake_kinds import ACCOUNT, Notebook, NotebookTracker
from harness import READY, AgentScript, Harness, run_id_of

LINK = "notebook:greeting"


def published(factory: Harness, **fields: Any) -> Notebook:
    """Publishes Spec `Greeting` with ready Tickets 1 to 3 on a Notebook, which the Harness takes as a Tracker kind."""
    tickets = [
        TicketSnapshot("1", "Greet", "Write hello.txt."),
        TicketSnapshot("2", "Greet again", "Write bye.txt."),
        TicketSnapshot("3", "Greet later", "Write later.txt."),
    ]
    for ticket in tickets:
        # Two scripts each, since a resumed Run starts the Attempt it lost again.
        script = AgentScript(files={f"{ticket.id}.txt": "hello\n"}, output={"summary": ticket.title, "handoff": "done"})
        factory.executor.ticket_scripts[(ticket.id, "implement")] = [script, script]
    labels = {ticket.id: [READY] for ticket in tickets}
    notebook = Notebook(LINK, "Greeting", "Say hello.", tickets, labels=labels, **fields)
    factory.registries.trackers.register("notebook", notebook.kind())
    return notebook


def test_a_run_assigns_each_ticket_it_takes_and_only_those_before_the_first_attempt_and_leaves_them_assigned(
    factory: Harness,
) -> None:
    notebook = published(factory, assignees={"1": ["mona"], "2": [ACCOUNT]})

    result = factory.run(LINK, "--tickets", "1,2")

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "TicketAssigned") == [{"ticket": "1", "assignee": ACCOUNT}]
    assert notebook.assignees == {"1": ["mona", ACCOUNT], "2": [ACCOUNT]}
    kinds = [kind for kind, _ in factory.all_events(run_id)]
    assert kinds.index("TicketAssigned") < kinds.index("AttemptStarted")


def test_a_failing_assignment_is_journaled_and_shown_and_the_run_goes_on(factory: Harness) -> None:
    notebook = published(factory, assign_error="no rights on this Tracker")

    result = factory.run(LINK, "--tickets", "1")

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "TicketAssignFailed") == [{"ticket": "1", "reason": "no rights on this Tracker"}]
    assert "#1 is not assigned: no rights on this Tracker" in result.out
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"
    assert notebook.assignees == {}


def test_a_run_on_a_tracker_without_a_ticket_board_assigns_nothing(factory: Harness) -> None:
    notebook = published(factory)

    def without_board(link: SpecLink, tools: Any) -> Tracker:
        tracker = NotebookTracker(notebook, link.tickets)
        return Tracker(tracker, tracker)

    factory.registries.trackers.register("notebook", Kind(without_board, picks=lambda spec: spec == LINK))

    result = factory.run(LINK, "--tickets", "1")

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "TicketAssigned") == factory.events(run_id, "TicketAssignFailed") == []


def test_a_run_on_a_spec_folder_assigns_nothing_since_a_folder_has_no_accounts(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": "---\ntitle: Greet\n---\nWrite hello.txt.\n"})
    factory.executor.scripts["implement"] = AgentScript(
        files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"}
    )

    result = factory.run(str(spec))

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "TicketAssigned") == factory.events(run_id, "TicketAssignFailed") == []
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"
