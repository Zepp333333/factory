"""Deciders of kind `function` are code of the factory that choose a Decision's outcome from its inputs.

Each function declares the schema of its `with` and a pure judgment of the resolved `with` and the outputs of the
Stages the Decision reads, and the Orchestrator carries out the outcome it returns.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from factory.gates import REQUIRED, Field
from factory.values import Invalid

# The reviewer's `door` and `blast_radius` grow with the danger of a change, and a threshold is the first risky value.
DOORS = ("two_way", "one_way")
BLAST_RADII = ("local", "module", "cross_module", "external")


@dataclass(frozen=True)
class Judgment:
    outcome: str
    reasons: list[str]


@dataclass(frozen=True)
class Function:
    fields: dict[str, Field]
    outcome_fields: tuple[str, ...]
    judge: Callable[[dict[str, Any], list[dict[str, Any]]], Judgment]


def resolve_with(function: str, raw: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Checks `with` against the function's schema and fills in defaults.

    Returns the resolved `with` and the errors as pairs of the field and the message.
    """
    fields = FUNCTIONS[function].fields
    errors = [(key, "unknown field") for key in raw if key not in fields]
    resolved = {}
    for key, field in fields.items():
        if key not in raw and field.default is REQUIRED:
            errors.append(("", f"missing {key}"))
            continue
        try:
            resolved[key] = field.parse(raw.get(key, field.default))
        except Invalid as error:
            errors.append((key, str(error)))
    return resolved, errors


def outcomes_of(function: str, with_: dict[str, Any]) -> list[str]:
    """Returns the outcomes the function may give for its resolved `with`, which a Decision must declare."""
    return [str(with_[key]) for key in FUNCTIONS[function].outcome_fields]


def judge(function: str, with_: dict[str, Any], outputs: list[dict[str, Any]]) -> Judgment:
    """Returns the function's outcome and reasons for the outputs of the Stages the Decision reads, oldest first."""
    return FUNCTIONS[function].judge(with_, outputs)


def _choice(options: tuple[str, ...]) -> Callable[[Any], str]:
    def parse(value: Any) -> str:
        if value not in options:
            raise Invalid(f"must be one of {', '.join(options)}")
        return str(value)

    return parse


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise Invalid("must name an outcome")
    return value


def _risk(with_: dict[str, Any], outputs: list[dict[str, Any]]) -> Judgment:
    """Sends a change on as `risky` when the reviewer's `door` or `blast_radius` reaches its threshold.

    The latest output that carries both fields counts, and a Decision with no such output escalates.
    """
    review = next(
        (o for o in reversed(outputs) if o.get("door") in DOORS and o.get("blast_radius") in BLAST_RADII), None
    )
    if review is None:
        return Judgment("escalate", ["no Stage output carries a valid door and blast_radius"])
    reasons = []
    if DOORS.index(review["door"]) >= DOORS.index(with_["door"]):
        reasons.append(f"door is {review['door']}, and the threshold is {with_['door']}")
    if BLAST_RADII.index(review["blast_radius"]) >= BLAST_RADII.index(with_["blast_radius"]):
        reasons.append(f"blast_radius is {review['blast_radius']}, and the threshold is {with_['blast_radius']}")
    if reasons:
        return Judgment(with_["risky"], [*reasons, *_notes(review)])
    return Judgment(
        with_["safe"],
        [f"door {review['door']} and blast_radius {review['blast_radius']} are below the thresholds"],
    )


def _notes(review: dict[str, Any]) -> list[str]:
    notes = review.get("danger_notes")
    return [str(notes)] if notes else []


FUNCTIONS: dict[str, Function] = {
    "risk": Function(
        fields={
            "door": Field(_choice(DOORS), "one_way"),
            "blast_radius": Field(_choice(BLAST_RADII), "cross_module"),
            "risky": Field(_name, "risky"),
            "safe": Field(_name, "safe"),
        },
        outcome_fields=("risky", "safe"),
        judge=_risk,
    )
}
