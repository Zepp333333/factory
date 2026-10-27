"""Deciders of kind `function` are code of the factory that choose a Decision's outcome from its inputs.

Each function declares the schema of its `with` and decides, as a pure function of the resolved `with` and the
outputs of the Stages the Decision reads, a Ruling that the Orchestrator carries out.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from factory.gates import REQUIRED, Field
from factory.values import Invalid, choice

# The reviewer's `door` and `blast_radius` grow with the danger of a change, and a threshold is the first risky value.
DOORS = ("two_way", "one_way")
BLAST_RADII = ("local", "module", "cross_module", "external")


@dataclass(frozen=True)
class Ruling:
    """A Ruling is the outcome a Decider picked for a Decision, with its reasons."""

    outcome: str
    reasons: list[str]


@dataclass(frozen=True)
class Function:
    fields: dict[str, Field]
    outcome_fields: tuple[str, ...]
    decide: Callable[[dict[str, Any], list[dict[str, Any]]], Ruling]


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


def decide(function: str, with_: dict[str, Any], outputs: list[dict[str, Any]]) -> Ruling:
    """Returns the function's Ruling on the outputs of the Stages the Decision reads, oldest first."""
    return FUNCTIONS[function].decide(with_, outputs)


def _outcome(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise Invalid("must name an outcome")
    return value


def _risk(with_: dict[str, Any], outputs: list[dict[str, Any]]) -> Ruling:
    """Sends a change on as `risky` when the reviewer's `door` or `blast_radius` reaches its threshold.

    The latest output that carries both fields counts, and a Decision with no such output escalates.
    """
    review = next(
        (o for o in reversed(outputs) if o.get("door") in DOORS and o.get("blast_radius") in BLAST_RADII), None
    )
    if review is None:
        return Ruling("escalate", ["no Stage output carries a valid door and blast_radius"])
    reasons = []
    if DOORS.index(review["door"]) >= DOORS.index(with_["door"]):
        reasons.append(f"door is {review['door']}, and the threshold is {with_['door']}")
    if BLAST_RADII.index(review["blast_radius"]) >= BLAST_RADII.index(with_["blast_radius"]):
        reasons.append(f"blast_radius is {review['blast_radius']}, and the threshold is {with_['blast_radius']}")
    if reasons:
        return Ruling(with_["risky"], [*reasons, *_notes(review)])
    return Ruling(
        with_["safe"],
        [f"door {review['door']} and blast_radius {review['blast_radius']} are below the thresholds"],
    )


def _notes(review: dict[str, Any]) -> list[str]:
    notes = review.get("danger_notes")
    return [str(notes)] if notes else []


FUNCTIONS: dict[str, Function] = {
    "risk": Function(
        fields={
            "door": Field(choice(DOORS), "one_way"),
            "blast_radius": Field(choice(BLAST_RADII), "cross_module"),
            "risky": Field(_outcome, "risky"),
            "safe": Field(_outcome, "safe"),
        },
        outcome_fields=("risky", "safe"),
        decide=_risk,
    )
}
