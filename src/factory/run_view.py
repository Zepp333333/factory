"""Run view: the text tree of a Run, its Tickets and their current node, printed by the Run's process."""

from typing import TextIO

from factory.journal import Journal, TicketRow

ICONS = {"queued": ".", "active": "~", "merged": "+", "abandoned": "x", "stuck": "!", "done": "+"}


def render_tree(journal: Journal, run_id: str) -> str:
    run = journal.run(run_id)
    assert run is not None
    lines = [f"Run {run.id}  [{run.state}]  {run.integration_branch}"]
    for ticket in journal.tickets(run_id):
        lines.append(f"  {ICONS.get(ticket.state, '?')} {ticket.id:<12} {ticket.node or '-':<12} {_where(ticket)}")
    return "\n".join(lines) + "\n"


def _where(ticket: TicketRow) -> str:
    if ticket.state == "queued":
        return "queued"
    if ticket.stage is None:
        return ticket.state
    return f"{ticket.state}, {ticket.stage} round {ticket.round}, attempt {ticket.attempt}"


class RunView:
    """Prints the tree whenever it changes, redrawing in place on a TTY and appending otherwise."""

    def __init__(self, out: TextIO) -> None:
        self._out = out
        self._last = ""

    def show(self, journal: Journal, run_id: str) -> None:
        tree = render_tree(journal, run_id)
        if tree == self._last:
            return
        if self._last and self._out.isatty():
            height = self._last.count("\n")
            self._out.write(f"\x1b[{height}F\x1b[J")
        self._last = tree
        self._out.write(tree)
        self._out.flush()
