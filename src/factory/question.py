"""The question card is what a human Decider reads before it answers a Decision."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Link:
    """A Link points at an Artifact, such as a Stage output or a Gate verdict, with what it holds in a few words."""

    label: str
    path: Path


@dataclass(frozen=True)
class CardOutcome:
    """A CardOutcome is an outcome the card offers, with what it means and whether it needs reasons."""

    name: str
    description: str
    needs_reasons: bool


def render_card(
    run_id: str,
    ticket: str,
    title: str,
    decision: str,
    cause: Link | None,
    verdicts: list[Link],
    inputs: list[Link],
    outcomes: list[CardOutcome],
) -> str:
    """Renders the card: the Ticket, the cause, the latest verdicts, links to the inputs and the outcomes."""
    lines = [f"# Decision {decision} of Ticket {ticket}: {title}", "", f"Run: {run_id}", ""]
    lines += ["## Cause", ""]
    lines += [_link(cause)] if cause else ["None."]
    lines += ["", "## Latest verdicts", ""]
    lines += [_link(v) for v in verdicts] or ["None."]
    lines += ["", "## Inputs", ""]
    lines += [_link(i) for i in inputs] or ["None."]
    lines += ["", "## Outcomes", ""]
    for outcome in outcomes:
        head = f"- {outcome.name}{' (needs reasons)' if outcome.needs_reasons else ''}"
        lines.append(f"{head}: {outcome.description}" if outcome.description else head)
    lines += ["", "Answer with:", "", f"    factory decide {run_id} {ticket} <outcome> --reason <why>", ""]
    return "\n".join(lines)


def _link(link: Link) -> str:
    return f"- {link.label}: {link.path}{_gist(link.path)}"


def _gist(path: Path) -> str:
    """Returns the outcome and reasons an Artifact records, when it is a JSON file that holds them."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    parts = [f"outcome {data['outcome']}"] if isinstance(data.get("outcome"), str) else []
    reasons = data.get("reasons")
    if isinstance(reasons, list) and reasons:
        parts.append("; ".join(str(r) for r in reasons))
    return f" ({', '.join(parts)})" if parts else ""
