"""The inputs module tells what a Fresh agent reads and checks what it gives back.

A Stage and a model Decider both list their inputs in the Brief, and a Stage, the Describer and the CI fixer all
check their output against the schema of their Role.
"""

import json
from pathlib import Path
from typing import Any

import jsonschema

from factory.journal.artifacts import RunDir
from factory.pipeline.pipeline import TRACKER_INPUTS, Input
from factory.run.live import LiveTicket


def schema_errors(role_output: Path, output: dict[str, Any] | None) -> list[str]:
    """Returns the ways an output breaks the Role's schema, which is none for no output."""
    if output is None:
        return []
    schema = json.loads(role_output.read_text())
    schema_check = jsonschema.Draft202012Validator(schema)
    return [error.message for error in schema_check.iter_errors(output)]


def comments_in(brief: dict[str, Any]) -> list[str]:
    """Returns the ids of the comments a Brief holds, which the Stage has handled once its Attempt is done."""
    return [comment["id"] for entry in brief["inputs"] for comment in entry.get("comments", [])]


def resolve_input(run_dir: RunDir, ticket: LiveTicket, item: Input) -> list[dict[str, Any]]:
    """Returns the Brief entries of one input, which is empty only for an optional input with no artifact yet.

    A Stage's entry carries the commit range of its Round, so the agent reads that code with `git diff`.
    An input from the Tracker holds the new comments, which were read before the Attempt took its slot.
    """
    if item.source in TRACKER_INPUTS:
        return [{"source": item.source, "comments": (ticket.heard or {}).get(item.source, [])}]
    if item.source == "spec":
        return [{"source": "spec", "path": str(run_dir.spec)}]
    if item.source == "ticket":
        return [{"source": "ticket", "path": str(run_dir.ticket(ticket.id))}]
    if item.source == "cause":
        assert ticket.cause is not None, "Config proves that cause has a source on every path"
        return [{"source": "cause", **ticket.cause}]
    found = [a for a in ticket.artifacts if a["kind"] == item.source and a["node"] in item.nodes]
    assert found or item.optional, f"Config proves that {item} has a source on every path"
    picked = found if item.all_rounds else found[-1:]
    return [{"source": item.source, **{k: v for k, v in a.items() if k != "kind"}} for a in picked]
