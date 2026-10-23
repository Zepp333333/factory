"""Gate kinds judge a Probe's reports on the host, so a verdict never depends on code the agent wrote.

Each kind declares the schema of its `with`, the Probe it runs and an evaluator.
The evaluator is a pure function of the resolved `with` and the Probe's result, and `judge` wraps
what it returns into the verdict artifact every kind shares.
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.values import Invalid, duration, non_negative

# These bounds keep a summary small enough for the fix agent to read in full.
MAX_MATCHED = 20
MAX_TAIL = 200
MAX_LINE = 500
MAX_FAILED = 20
MAX_MESSAGE = 300
MAX_WHERE = 200
TESTS_TAIL = 20


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
class Probe:
    """A Probe is the command a Gate runs in the Ticket's Sandbox and how long it may run."""

    argv: list[str]
    timeout: float


@dataclass(frozen=True)
class Evaluation:
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
    probe: Callable[[dict[str, Any]], Probe]
    evaluate: Callable[[dict[str, Any], ProbeResult], Evaluation]


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


def probe(kind: str, with_: dict[str, Any]) -> Probe:
    """Returns the Probe a Gate of `kind` runs for its resolved `with`."""
    return KINDS[kind].probe(with_)


def judge(gate: str, kind: str, with_: dict[str, Any], probe: ProbeResult) -> dict[str, Any]:
    """Returns the verdict artifact of a Gate on a finished Probe."""
    evaluation = KINDS[kind].evaluate(with_, probe)
    return {
        "gate": gate,
        "kind": kind,
        "outcome": "pass" if evaluation.passed else "fail",
        "reasons": evaluation.reasons,
        "summary": evaluation.summary,
        "log": str(probe.log),
        "exit_code": probe.exit_code,
        "duration": probe.duration,
    }


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


def _lines(log: Path) -> list[str]:
    return [line[:MAX_LINE] for line in log.read_text(errors="replace").splitlines()]


def _command_probe(with_: dict[str, Any]) -> Probe:
    # The shell sends the command's stderr to the log too, whichever Sandbox runs it.
    return Probe(argv=["sh", "-c", f"exec 2>&1\n{with_['run']}"], timeout=with_["timeout"])


def _command(with_: dict[str, Any], probe: ProbeResult) -> Evaluation:
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
    return Evaluation(
        passed=not reasons,
        reasons=reasons,
        summary={"matched": matched, "tail": lines[len(lines) - tail :] if tail else []},
    )


def _gate_out_command(value: Any) -> str:
    run = _string(value)
    if "GATE_OUT" not in run:
        raise Invalid("must write its report to $GATE_OUT")
    return run


def _report_path(value: Any) -> str:
    path = Path(_string(value))
    if path.is_absolute() or ".." in path.parts:
        raise Invalid("must be a path inside $GATE_OUT")
    return str(path)


def _junit(files: list[Path]) -> tuple[int, list[dict[str, str]]]:
    """Returns the number of test cases in the JUnit `files` and the failed ones with their message and place."""
    total = 0
    failed = []
    for file in files:
        for case in ET.parse(file).getroot().iter("testcase"):
            total += 1
            bad = case.find("failure")
            if bad is None:
                bad = case.find("error")
            if bad is not None:
                failed.append(
                    {
                        "test": f"{case.get('classname')}::{case.get('name')}",
                        "message": (bad.get("message") or "")[:MAX_MESSAGE],
                        "where": (bad.text or "").strip()[:MAX_WHERE],
                    }
                )
    return total, failed


def _tests(with_: dict[str, Any], probe: ProbeResult) -> Evaluation:
    """Passes only on a green JUnit report with a zero exit code and sums up the failed tests.

    A timeout, a missing or unreadable report, a report without tests and a non-zero exit code
    with a green report are all code-caused, so they fail.
    """
    files = sorted(path for path in probe.gate_out.glob(with_["junit"]) if path.is_file())
    total = 0
    failed: list[dict[str, str]] = []
    unreadable = ""
    try:
        total, failed = _junit(files)
    except ET.ParseError as error:
        unreadable = f"{with_['junit']} is not a JUnit report: {error}"
    reasons = []
    if probe.timed_out:
        reasons.append(f"timed out after {with_['timeout']:g}s")
    elif unreadable:
        reasons.append(unreadable)
    elif not files:
        reasons.append(f"no JUnit report at $GATE_OUT/{with_['junit']}")
    elif failed:
        reasons.append(f"{len(failed)} of {total} tests failed")
    elif total == 0:
        reasons.append("the report has no tests")
    elif probe.exit_code != 0:
        reasons.append(f"exit code {probe.exit_code} but the report has no failed tests")
    summary: dict[str, Any] = {"total": total, "failed": failed[:MAX_FAILED]}
    if reasons:
        lines = _lines(probe.log)
        summary["tail"] = lines[len(lines) - TESTS_TAIL :]
    return Evaluation(passed=not reasons, reasons=reasons, summary=summary)


KINDS = {
    "command": Kind(
        fields={
            "run": Field(_string),
            "timeout": Field(duration, "30m"),
            "match": Field(_pattern, r"\berror\b"),
            "tail": Field(non_negative, 40),
        },
        probe=_command_probe,
        evaluate=_command,
    ),
    "tests": Kind(
        fields={
            "run": Field(_gate_out_command),
            "junit": Field(_report_path),
            "timeout": Field(duration, "30m"),
        },
        probe=_command_probe,
        evaluate=_tests,
    ),
}
