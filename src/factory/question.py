"""The question card is what a human Decider reads before it answers a Decision."""

import json
from dataclasses import dataclass
from pathlib import Path

from factory.commands import RUN_LEVEL
from factory.states import OOM, OOM_ADVICE
from factory.tracker import FailedJob


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


def render_run_card(run_id: str, decision: str, cause: str, failed: list[str], outcomes: dict[str, str]) -> str:
    """Renders the card of a Run-level Decision: the cause class, the Attempts that failed with it and the outcomes."""
    lines = [f"# Decision {decision} of Run {run_id}", "", "## Cause", ""]
    if cause == OOM:
        lines += [f"The Sandbox's OOM killer killed the agent of two Attempts in a row; {OOM_ADVICE}.", ""]
    else:
        lines += [f"Attempts of at least two Tickets failed in a row with {cause}.", ""]
    lines += [f"- {line}" for line in failed]
    lines += ["", "## Outcomes", ""]
    lines += [f"- {name}: {description}" for name, description in outcomes.items()]
    lines += ["", "Answer with:", "", f"    factory decide {run_id} {RUN_LEVEL} <outcome>", ""]
    return "\n".join(lines)


def render_ci_card(
    run_id: str, decision: str, outcome: str, commit: str, jobs: list[str], log: Path | None, outcomes: dict[str, str]
) -> str:
    """Renders the card of the Decision on red CI: how CI ended on the commit, its failed jobs and the outcomes."""
    ended = "did not end in time" if outcome == "timeout" else "failed"
    lines = [
        f"# Decision {decision} of Run {run_id}",
        "",
        "## Cause",
        "",
        f"CI on the PR's commit {commit} {ended}.",
        "",
    ]
    lines += [f"- {job}" for job in jobs]
    if log is not None:
        lines += ["", f"The tails of the failed logs: {log}"]
    lines += ["", "## Outcomes", ""]
    lines += [f"- {name}: {description}" for name, description in outcomes.items()]
    lines += ["", "Answer with:", "", f"    factory decide {run_id} {RUN_LEVEL} <outcome>", ""]
    return "\n".join(lines)


def render_failed_logs(jobs: list[FailedJob]) -> str:
    """Renders the Artifact of the failed jobs: for each its name, its URL and the tail of its log."""
    return "".join(f"# {job.name}\n\n{job.url}\n\n```\n{job.log}\n```\n\n" for job in jobs)
