"""The parse of a Tracker Operation's block into its TrackerAction, which a Pipeline and a Run graph share."""

import re
from collections.abc import Mapping
from typing import Any

from factory.core import values
from factory.pipeline.pipeline import (
    ASSIGN,
    COMMENT,
    COMMENT_REFERENCES,
    ITEM_REFERENCE,
    LABEL,
    ON_FAIL,
    SPEC_TARGET,
    WARN,
    TrackerAction,
)
from factory.pipeline.run_kinds import Report, mapping, names, parsed

# The fields of the `with` of each Tracker Operation.
TRACKER_WITH_FIELDS = {ASSIGN: (), LABEL: ("add", "remove"), COMMENT: ("text",)}
# What the `with` of a `comment` in a Pipeline names to take its text from a Stage, which a Run node has none of.
COMMENT_FROM_FIELDS = ("from", "field", "each")

REFERENCE = re.compile(r"\$\{([^}]*)\}")


def parse(where: str, body: Mapping[str, Any], kind: str, at_run: bool, report: Report) -> TrackerAction:
    """Reads what a Tracker Operation of `kind` does.

    Only a Run node has a `target`, because in a Pipeline the Operation acts on its own Ticket.
    """
    at = f"{where}.with"
    extra = ("target",) if at_run else COMMENT_FROM_FIELDS if kind == COMMENT else ()
    with_ = mapping(at, body.get("with", {}), report)
    for key in with_:
        if key not in (*TRACKER_WITH_FIELDS[kind], *extra):
            report(f"{at}.{key}", "unknown field")
    on_spec = "target" in with_
    if on_spec and with_["target"] != SPEC_TARGET:
        report(f"{at}.target", f"must be {SPEC_TARGET}")
    on_fail = parsed(f"{where}.on_fail", body.get("on_fail", WARN), values.choice(ON_FAIL), report) or WARN
    add, remove = names(at, with_, "add", report), names(at, with_, "remove", report)
    if kind == LABEL and not add and not remove:
        report(at, "names no label to add or remove")
    source = _comment_source(at, with_, report)
    text = _comment_text(at, with_, on_spec, source[0] is not None, report) if kind == COMMENT else ""
    return TrackerAction(kind, add, remove, text, on_spec, on_fail, *source)


def _comment_source(at: str, with_: Mapping[str, Any], report: Report) -> tuple[str | None, str, bool]:
    """Reads the Stage, the field of its output and the `each` of a `comment` that takes its text from a Stage."""
    source = with_.get("from")
    if source is None:
        for key in ("field", "each"):
            if key in with_:
                report(f"{at}.{key}", "only a comment with from has it")
        return None, "", False
    field = with_.get("field")
    if not isinstance(field, str) or not field:
        report(f"{at}.field", "must name a field of the output of the Stage")
        field = ""
    each = with_.get("each", False)
    if not isinstance(each, bool):
        report(f"{at}.each", "must be true or false")
    return str(source), field, each is True


def _comment_text(at: str, with_: Mapping[str, Any], on_spec: bool, from_stage: bool, report: Report) -> str:
    """Reads the text of a `comment`, which may name only the references a comment on its target has.

    A `comment` from a Stage may leave it out, and may name the fields of its items, which the check holds against
    the Role of the Stage.
    """
    text = with_.get("text", "" if from_stage else None)
    if from_stage and text == "":
        return ""
    if not isinstance(text, str) or not text:
        report(f"{at}.text", "must be a text")
        return ""
    for name in REFERENCE.findall(ITEM_REFERENCE.sub("", text) if from_stage else text):
        if name not in COMMENT_REFERENCES:
            report(f"{at}.text", f"unknown reference ${{{name}}}")
        elif name == "ticket" and on_spec:
            report(f"{at}.text", "a comment on the Spec has no reference ${ticket}")
    return text
