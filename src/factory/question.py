"""The question card is what a human Decider reads before it answers a Decision."""

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Link:
    """A Link points at an Artifact, such as a Stage output or a Gate verdict, with what it holds in a few words."""

    label: str
    path: Path


def render_card(
    run_id: str,
    ticket: str,
    title: str,
    decision: str,
    cause: Link | None,
    verdicts: list[Link],
    inputs: list[Link],
    outcomes: list[tuple[str, str, bool]],
) -> str:
    """Renders the card: the Ticket, the cause, the latest verdicts, links to the inputs and the outcomes.

    `outcomes` lists each outcome with its description and whether it sends the Ticket back to work,
    which makes it need reasons.
    """
    lines = [f"# Decision {decision} of Ticket {ticket}: {title}", "", f"Run: {run_id}", ""]
    lines += ["## Cause", ""]
    lines += [_link(cause)] if cause else ["None."]
    lines += ["", "## Latest verdicts", ""]
    lines += [_link(v) for v in verdicts] or ["None."]
    lines += ["", "## Inputs", ""]
    lines += [_link(i) for i in inputs] or ["None."]
    lines += ["", "## Outcomes", ""]
    for name, description, needs_reasons in outcomes:
        note = " (needs reasons)" if needs_reasons else ""
        lines.append(f"- {name}{note}: {description}" if description else f"- {name}{note}")
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
    parts = [f"{key} {data[key]}" for key in ("outcome",) if isinstance(data.get(key), str)]
    reasons = data.get("reasons")
    if isinstance(reasons, list) and reasons:
        parts.append("; ".join(str(r) for r in reasons))
    return f" ({', '.join(parts)})" if parts else ""
