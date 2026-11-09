"""The kinds `all` and `filter` select the Tickets of the Run from those the Tracker read."""

from collections.abc import Mapping
from typing import Any

from factory.core.events import Event, applies
from factory.journal.run_state import RunMemory
from factory.pipeline.pipeline import EMPTY, FILTER, SELECT, SELECTED, TICKETS, RunNode
from factory.pipeline.run_kinds import AT_MOST_ONE, NodeRunner, Order, Report, RunNodeKind, fixed, names, with_block
from factory.run.node_tools import NodeTools

# The kind that selects all the Tickets the Tracker read.
ALL = "all"

ORDER = Order(count=AT_MOST_ONE, group=SELECT, before=(TICKETS,))


def parse_all(where: str, body: Mapping[str, Any], report: Report) -> dict[str, Any]:
    with_block(where, body, report)
    return {}


def parse_filter(where: str, body: Mapping[str, Any], report: Report) -> dict[str, Any]:
    """Reads the `with` of a `filter`, whose `label` and `without` each name one label or a list of them."""
    block = with_block(where, body, report, optional=("label", "without"))
    lists = {key: [value] if isinstance(value, str) else value for key, value in block.fields.items()}
    found = {key: list(names(block.where, lists, key, report)) for key in ("label", "without")}
    if not found["label"] and not found["without"]:
        report(block.where, "names no label to take or leave out")
    return found


def filter_labels(node: Any) -> tuple[str, ...]:
    return (*node.settings["label"], *node.settings["without"])


class SelectRunner(NodeRunner):
    """Select takes the Tickets the node keeps of those the Tracker read, once, and ends in whether it took any."""

    def __init__(self, node: RunNode, tools: NodeTools) -> None:
        self.node = node
        self.tools = tools

    def step(self) -> str:
        context = self.tools.context
        if not context.memory.selected:
            settings = self.node.settings
            taken = context.read.carrying(settings.get("label", ()), settings.get("without", ()))
            take(self.tools, self.node.name, {"with": settings}, [t.id for t in taken], {})
        return SELECTED if context.tickets else EMPTY


def take(tools: NodeTools, node: str, judged_on: dict[str, Any], taken: list[str], reasons: dict[str, str]) -> None:
    """Narrows the Run to the Tickets `taken`, in that order, and warns of each it leaves out, with its reason.

    The Artifact holds them, each with what blocks it, and the rest it left out, and the event that narrows the Run
    to them carries what it holds and what the node judged on, so a Run that resumes does not select again.
    A Ticket blocked by one the Run left out, or by one the Tracker did not read, stays queued and never starts.
    """
    context = tools.context
    read = {t.id: t for t in context.read.tickets}
    left_out = [t for id, t in read.items() if id not in taken]
    selection = {
        "tickets": [{"id": id, "blocked_by": [*read[id].blocked_by, *read[id].blocked_outside]} for id in taken],
        "left_out": [t.id for t in left_out],
        **({"reasons": reasons} if reasons else {}),
    }
    artifact = context.run_dir.write_selection(selection)
    payload = {"node": node, **judged_on, "artifact": str(artifact), **selection}
    tools.run_tools.append(Event.TicketsSelected, payload)
    for ticket in left_out:
        why = reasons.get(ticket.id)
        carried = f": {why}" if why else f", which carries {', '.join(ticket.labels) or 'no label'}"
        tools.run_tools.view.warn(f"leaves out {context.closing.reference(ticket.id)} {ticket.title}{carried}")


ALL_KIND = RunNodeKind(name=ALL, outcomes=fixed(SELECTED, EMPTY), parse=parse_all, order=ORDER, make=SelectRunner)
FILTER_KIND = RunNodeKind(
    name=FILTER,
    outcomes=fixed(SELECTED, EMPTY),
    parse=parse_filter,
    order=ORDER,
    labels=filter_labels,
    make=SelectRunner,
)


# What the event of a selection tells the readers of the Journal.


@applies(Event.TicketsSelected)
def _apply_tickets_selected(memory: RunMemory, payload: dict[str, Any]) -> None:
    tickets = {t.id: t for t in memory.tickets}
    memory.tickets = [tickets[taken["id"]] for taken in payload["tickets"]]
    for ticket, taken in zip(memory.tickets, payload["tickets"], strict=True):
        ticket.blocked_by = taken["blocked_by"]
    memory.selected = True
