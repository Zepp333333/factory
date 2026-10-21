"""Gate kinds judge a Probe's reports on the host, so a verdict never depends on code the agent wrote.

Each kind declares the schema of its `with` and an evaluator.
The evaluator is a pure function of the resolved `with` and the Probe's result, and `judge` wraps
what it returns into the verdict artifact every kind shares.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DURATION = re.compile(r"(\d+)([smh])")
SECONDS = {"s": 1, "m": 60, "h": 3600}

# These bounds keep a summary small enough for the fix agent to read in full.
MAX_MATCHED = 20
MAX_TAIL = 200
MAX_LINE = 500


class Invalid(ValueError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    """A ProbeResult holds what a finished Probe left: its `$GATE_OUT`, its log and how it ended.

    `exit_code` is None when the Probe was killed on timeout.
    """

    gate_out: Path
    log: Path
    exit_code: int | None
    timed_out: bool
    duration: float


@dataclass(frozen=True)
class Judgment:
    passed: bool
    reasons: list[str]
    summary: dict[str, Any]


REQUIRED = object()


@dataclass(frozen=True)
class Field:
    parse: Callable[[Any], Any]
    default: Any = REQUIRED


@dataclass(frozen=True)
class Kind:
    fields: dict[str, Field]
    evaluate: Callable[[dict[str, Any], ProbeResult], Judgment]


def resolve_with(kind: str, raw: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Checks `with` against the kind's schema and fills in defaults.

    Returns the resolved `with` and the errors as pairs of the field and the message.
    """
    fields = KINDS[kind].fields
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


def judge(gate: str, kind: str, with_: dict[str, Any], probe: ProbeResult) -> dict[str, Any]:
    """Returns the verdict artifact of a Gate on a finished Probe."""
    judgment = KINDS[kind].evaluate(with_, probe)
    return {
        "gate": gate,
        "kind": kind,
        "outcome": "pass" if judgment.passed else "fail",
        "reasons": judgment.reasons,
        "summary": judgment.summary,
        "log": str(probe.log),
        "exit_code": probe.exit_code,
        "duration": probe.duration,
    }


def duration(value: Any) -> float:
    """Reads a positive duration given as seconds or as a number with a unit `s`, `m` or `h`."""
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    match = DURATION.fullmatch(value) if isinstance(value, str) else None
    if match is None or int(match.group(1)) == 0:
        raise Invalid("must be a duration such as 90s, 45m or 2h")
    return float(int(match.group(1)) * SECONDS[match.group(2)])


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise Invalid("must be a string")
    return value


def _pattern(value: Any) -> str:
    try:
        re.compile(_string(value))
    except re.error as error:
        raise Invalid(f"must be a regular expression: {error}") from error
    return str(value)


def _count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Invalid("must be a non-negative integer")
    return value


def _lines(log: Path) -> list[str]:
    return [line[:MAX_LINE] for line in log.read_text(errors="replace").splitlines()]


def _command(with_: dict[str, Any], probe: ProbeResult) -> Judgment:
    """Judges by the exit code and sums up the log by the lines `match` picks and the log's tail."""
    lines = _lines(probe.log)
    pattern = re.compile(with_["match"])
    matched = [line for line in lines if pattern.search(line)][:MAX_MATCHED]
    tail = min(with_["tail"], MAX_TAIL)
    reasons = []
    if probe.timed_out:
        reasons.append(f"timed out after {with_['timeout']:g}s")
    elif probe.exit_code != 0:
        reasons.append(f"exit code {probe.exit_code}")
    return Judgment(
        passed=not reasons,
        reasons=reasons,
        summary={"matched": matched, "tail": lines[len(lines) - tail :] if tail else []},
    )


KINDS = {
    "command": Kind(
        fields={
            "run": Field(_string),
            "timeout": Field(duration, "30m"),
            "match": Field(_pattern, r"\berror\b"),
            "tail": Field(_count, 40),
        },
        evaluate=_command,
    ),
}
