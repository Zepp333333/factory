"""Stats compares Runs and variants on the metrics of `factory.metrics`, as plain data and as text.

A row is one Stage, or one Stage and one value of a fingerprint field with `by`.
Its Attempts give the cost, the requests and the cache share, and the Tickets whose first Attempt at the Stage
had that fingerprint give the Rounds and the share that passed its Gates in the first Round.
"""

import math
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from factory.metrics import AttemptMetrics, RunMetrics, TicketMetrics
from factory.run_view import format_duration

# The fingerprint field of the Attempt that each `--by` groups on.
GROUPS = {"prompt": "prompt_sha", "model": "model", "pipeline": "pipeline_sha", "factory_version": "factory_version"}


def percentile(values: Sequence[float], share: float) -> float | None:
    """Returns the value at `share` of the way through the sorted values, between neighbours, or None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * share
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def rows(runs: Sequence[RunMetrics], by: str | None = None, stage: str | None = None) -> list[dict[str, Any]]:
    """Returns one row per Stage, or per Stage and value of the `by` field, over the Attempts and Tickets of `runs`."""
    field = GROUPS[by] if by else None
    attempts: dict[tuple[str, str | None], list[AttemptMetrics]] = {}
    tickets: dict[tuple[str, str | None], list[tuple[int, TicketMetrics]]] = {}
    for run in runs:
        for attempt in run.attempts:
            if stage is None or attempt.node == stage:
                attempts.setdefault((attempt.node, getattr(attempt, field) if field else None), []).append(attempt)
        for ticket in run.tickets:
            for node, rounds in ticket.rounds.items():
                if stage is not None and node != stage:
                    continue
                first = ticket.fingerprints.get(node, {})
                key = first.get(field) if field else None
                tickets.setdefault((node, key), []).append((rounds, ticket))
    found = []
    for key in sorted(attempts.keys() | tickets.keys(), key=lambda k: (k[0], k[1] or "")):
        found.append(_row(key, by, attempts.get(key, []), tickets.get(key, [])))
    return found


def _row(
    key: tuple[str, str | None],
    by: str | None,
    attempts: list[AttemptMetrics],
    tickets: list[tuple[int, TicketMetrics]],
) -> dict[str, Any]:
    def known(name: str) -> list[float]:
        return [value for a in attempts if (value := getattr(a, name)) is not None]

    cost, requests, seconds = known("cost_usd"), known("api_requests"), known("duration_s")
    cache = [a.cache_hit_ratio for a in attempts if a.cache_hit_ratio is not None]
    return {
        "stage": key[0],
        **({by: key[1]} if by else {}),
        "n_attempts": len(attempts),
        "n_tickets": len(tickets),
        "cost_usd_p50": percentile(cost, 0.5),
        "cost_usd_p90": percentile(cost, 0.9),
        "api_requests_p50": percentile(requests, 0.5),
        "api_requests_p90": percentile(requests, 0.9),
        "duration_s_p50": percentile(seconds, 0.5),
        "duration_s_p90": percentile(seconds, 0.9),
        "cache_hit_pct": 100 * sum(cache) / len(cache) if cache else None,
        "rounds_p50": percentile([rounds for rounds, _ in tickets], 0.5),
        "rounds_p90": percentile([rounds for rounds, _ in tickets], 0.9),
        "first_pass_pct": 100 * sum(t.first_pass for _, t in tickets) / len(tickets) if tickets else None,
    }


def run_row(run: RunMetrics) -> dict[str, Any]:
    """Returns the numbers of one Run, with its Tickets' outcomes counted."""
    outcomes = [t.outcome for t in run.tickets]
    return {
        "run": run.run_id,
        "state": run.state,
        "duration_s": run.duration_s,
        "cost_usd": run.cost_usd,
        "cost_unknown": run.cost_unknown,
        "tickets_merged": outcomes.count("merged"),
        "tickets_abandoned": outcomes.count("abandoned"),
        "tickets_by_hand": sum(t.by_hand for t in run.tickets),
        "first_pass_pct": 100 * sum(t.first_pass for t in run.tickets) / len(run.tickets) if run.tickets else None,
        "slots_full_pct": None if run.slots_full_share is None else 100 * run.slots_full_share,
        "wait_blockers_s": sum(t.wait_blockers_s for t in run.tickets),
        "wait_slot_s": sum(t.wait_slot_s for t in run.tickets),
        "wait_pause_s": sum(t.wait_pause_s for t in run.tickets),
        "wait_merge_window_s": sum(t.wait_merge_window_s for t in run.tickets),
        "utilization_spent": run.utilization_spent,
        "pipeline_sha": run.pipeline_sha,
        "kit_sha": run.kit_sha,
        "factory_version": run.factory_version,
    }


def report(runs: Sequence[RunMetrics], by: str | None = None, stage: str | None = None) -> dict[str, Any]:
    """Returns what `factory stats` prints as data: the Stage rows of each Run, or the pooled rows grouped `by`."""
    if by:
        return {"by": by, "stages": rows(runs, by, stage)}
    return {
        "runs": [
            {
                **run_row(run),
                "stages": rows([run], None, stage),
                "tickets": [asdict(t) for t in run.tickets],
                "attempts": [asdict(a) for a in run.attempts],
            }
            for run in runs
        ]
    }


def render(data: dict[str, Any]) -> list[str]:
    """Returns the lines of the text report."""
    if "by" in data:
        return _table(data["stages"], data["by"])
    lines: list[str] = []
    for run in data["runs"]:
        if lines:
            lines.append("")
        lines += _run_lines(run)
        lines += _table(run["stages"], None)
    return lines


def _run_lines(run: dict[str, Any]) -> list[str]:
    unknown = "+?" if run["cost_unknown"] else ""
    utilization = run["utilization_spent"]
    spent = ", ".join(f"{name} {100 * used:.0f}%" for name, used in sorted((utilization or {}).items())) or "?"
    full = "?" if run["slots_full_pct"] is None else f"{run['slots_full_pct']:.0f}%"
    waits = ", ".join(
        f"{name} {run[f'wait_{key}_s']:.0f}s"
        for name, key in (("blockers", "blockers"), ("slot", "slot"), ("pause", "pause"), ("window", "merge_window"))
    )
    return [
        f"Run {run['run']}  {run['state']}  {format_duration(run['duration_s'])}  ${run['cost_usd']:.2f}{unknown}",
        f"  tickets {run['tickets_merged']} merged, {run['tickets_abandoned']} abandoned,"
        f" {run['tickets_by_hand']} by hand"
        f"  first-pass {_pct(run['first_pass_pct'])}  slots full {full}",
        f"  waited {waits}  subscription {spent}",
    ]


def _table(stages: list[dict[str, Any]], by: str | None) -> list[str]:
    head = [
        "stage",
        *([by] if by else []),
        "n",
        "cost $ p50/p90",
        "api req p50",
        "cache %",
        "rounds p50",
        "first-pass %",
    ]
    body = [
        [
            row["stage"],
            *([str(row[by] if row[by] is not None else "?")] if by else []),
            str(row["n_attempts"]),
            f"{_num(row['cost_usd_p50'], '.2f')} / {_num(row['cost_usd_p90'], '.2f')}",
            _num(row["api_requests_p50"], ".0f"),
            _num(row["cache_hit_pct"], ".0f"),
            _num(row["rounds_p50"], ".0f"),
            _num(row["first_pass_pct"], ".0f"),
        ]
        for row in stages
    ]
    widths = [max(len(line[i]) for line in [head, *body]) for i in range(len(head))]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip()
        for line in [head, *body]
    ]


def _num(value: float | None, spec: str) -> str:
    return "?" if value is None else format(value, spec)


def _pct(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}%"
