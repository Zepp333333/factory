import pytest

from factory import config, deciders
from factory.decision import Asking, Rejected, check_asked, check_ruling

TICKET = Asking("Ticket 1", ("review", 2), False)
RUN = Asking("the Run", ("infra_failure", 1), False)


def test_an_answer_for_another_round_is_already_decided() -> None:
    with pytest.raises(Rejected, match="^already decided$"):
        check_asked(TICKET, "review", 1)


def test_an_answer_for_another_node_is_already_decided_for_the_run_too() -> None:
    with pytest.raises(Rejected, match="^already decided$"):
        check_asked(RUN, "review", 2)


def test_an_answer_that_names_no_decision_after_one_was_taken_is_already_decided() -> None:
    with pytest.raises(Rejected, match="^already decided$"):
        check_asked(Asking("Ticket 1", None, True), None, None)


def test_an_answer_that_names_no_decision_says_the_subject_waited_for_none() -> None:
    with pytest.raises(Rejected, match="as the Run waited for none"):
        check_asked(Asking("the Run", None, False), None, None)


def test_the_answer_that_names_the_waiting_decision_passes() -> None:
    check_asked(TICKET, "review", 2)


def test_an_outcome_that_is_none_of_the_decisions_is_refused() -> None:
    with pytest.raises(Rejected, match="outcome 'x' is not one of ship, escalate"):
        check_ruling("x", [], ("ship", "escalate"), lambda _: False)


def test_an_outcome_that_sends_the_ticket_back_to_work_needs_reasons() -> None:
    with pytest.raises(Rejected, match="outcome rework sends the Ticket back to work and needs reasons"):
        check_ruling("rework", [], ("rework", "ship"), lambda outcome: outcome == "rework")


def test_a_valid_outcome_becomes_the_ruling() -> None:
    assert check_ruling("rework", ["why"], ("rework",), lambda _: True) == deciders.Ruling("rework", ["why"])


def test_the_kind_table_says_what_each_decider_does() -> None:
    assert [name for name, kind in deciders.KINDS.items() if kind.waits] == ["human"]
    assert [name for name, kind in deciders.KINDS.items() if kind.runs_attempt] == ["model"]


def test_config_runs_only_the_runnable_decider_kinds() -> None:
    assert "consilium" in config.DECIDER_KINDS
    assert "consilium" not in config.RUNNABLE_DECIDER_KINDS
