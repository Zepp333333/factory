"""Commands are the requests that change a Run, which any process puts in the Journal's inbox.

The CLI and the Run's terminal build the same `decide` Command, and the Orchestrator of the Run accepts or rejects it.
"""

from factory.journal import Journal, WaitingRow


def submit_decide(
    journal: Journal,
    run_id: str,
    ticket: str,
    outcome: str,
    reasons: list[str],
    source: str,
    waiting: WaitingRow | None,
) -> int:
    """Puts an answer to the Decision of `ticket` in the inbox and returns the Command's id.

    The Command names the Decision `waiting` it answers, so an answer that comes after another one is rejected
    as already decided even when the Ticket waits at a later Decision by then.
    """
    payload = {
        "ticket": ticket,
        "outcome": outcome,
        "reasons": reasons,
        "source": source,
        "node": waiting.node if waiting else None,
        "round": waiting.round if waiting else None,
    }
    return journal.submit_command(run_id, "decide", payload)
