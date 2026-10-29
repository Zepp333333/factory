"""Gate kinds judge a Probe's reports on the host, so a verdict never depends on code the agent wrote.

Each kind declares the schema of its `with`, the Probe it runs and an evaluator.
The evaluator is a pure function of the resolved `with` and the Probe's result, and `evaluate` wraps
what it returns into the verdict artifact every kind shares.
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from factory.values import Invalid, choice, duration, non_negative

# These bounds keep a summary small enough for the fix agent to read in full.
MAX_MATCHED = 20
MAX_TAIL = 200
MAX_LINE = 500
MAX_FAILED = 20
MAX_MESSAGE = 300
MAX_WHERE = 200
TESTS_TAIL = 20
MAX_UNCOVERED = 50
FORMATS = ("lcov", "cobertura", "jacoco")


@dataclass(frozen=True)
class ProbeResult:
    """A ProbeResult holds what a finished Probe left: its `$GATE_OUT`, its log and how it ended.

    `exit_code` is None when the Probe was killed on timeout.
    `changed` holds the lines the Ticket changed since its `base`, by file, for the kinds that judge changed lines.
    """

    gate_out: Path
    log: Path
    exit_code: int | None
    timed_out: bool
    duration: float
    changed: dict[str, frozenset[int]] = field(default_factory=dict)


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
    """A Kind declares the schema of its `with`, the Probe it runs and its evaluator.

    `check` tells what is wrong with a `with` whose fields are each valid, such as a missing one of two thresholds.
    `evaluates_changed_lines` makes the Orchestrator hand the evaluator the lines the Ticket changed.
    `reads_from` lets a Gate of the kind judge another Gate's Probe with `from` instead of running its own.
    """

    fields: dict[str, Field]
    probe: Callable[[dict[str, Any]], Probe]
    evaluate: Callable[[dict[str, Any], ProbeResult], Evaluation]
    check: Callable[[dict[str, Any]], list[str]] = lambda with_: []
    evaluates_changed_lines: bool = False
    reads_from: bool = False


def resolve_with(kind: str, raw: dict[str, Any]) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Checks `with` against the kind's schema and fills in defaults.

    Returns the resolved `with` and the errors as pairs of the field and the message.
    """
    fields = KINDS[kind].fields
    errors = [(key, "unknown field") for key in raw if key not in fields]
    resolved = {}
    for key, spec in fields.items():
        if key not in raw and spec.default is REQUIRED:
            errors.append(("", f"missing {key}"))
            continue
        try:
            resolved[key] = spec.parse(raw.get(key, spec.default))
        except Invalid as error:
            errors.append((key, str(error)))
    if not errors:
        errors = [("", message) for message in KINDS[kind].check(resolved)]
    return resolved, errors


def probe(kind: str, with_: dict[str, Any]) -> Probe:
    """Returns the Probe a Gate of `kind` runs for its resolved `with`."""
    return KINDS[kind].probe(with_)


def evaluate(gate: str, kind: str, with_: dict[str, Any], probe: ProbeResult) -> dict[str, Any]:
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
    matched = [line for line in lines if pattern.search(line)]
    tail = min(with_["tail"], MAX_TAIL)
    reasons = []
    if probe.timed_out:
        reasons.append(f"timed out after {with_['timeout']:g}s")
    elif probe.exit_code != 0:
        reasons.append(f"exit code {probe.exit_code}")
    return Evaluation(
        passed=not reasons,
        reasons=reasons,
        summary={
            "matched": matched[:MAX_MATCHED],
            "matched_count": len(matched),
            "tail": lines[len(lines) - tail :] if tail else [],
        },
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


def _percent(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 100:
        raise Invalid("must be a percentage from 0 to 100")
    return float(value)


def _optional_command(value: Any) -> str:
    run = _string(value)
    return _gate_out_command(run) if run else run


Coverage = dict[str, dict[int, bool]]


def _mark(lines: dict[int, bool], number: str | None, hits: str | None) -> None:
    """Marks a line covered once any report entry for it has hits, since a report may list a line twice."""
    line = int(number or 0)
    lines[line] = lines.get(line, False) or int(hits or 0) > 0


def _lcov(file: Path, covered: Coverage) -> None:
    current: dict[int, bool] = {}
    for line in file.read_text(errors="replace").splitlines():
        if line.startswith("SF:"):
            current = covered.setdefault(line[3:].strip(), {})
        elif line.startswith("DA:"):
            number, hits, *_ = line[3:].split(",")
            _mark(current, number, hits)


def _cobertura(file: Path, covered: Coverage) -> None:
    for cls in ET.parse(file).getroot().iter("class"):
        current = covered.setdefault(cls.get("filename") or "", {})
        for line in cls.iter("line"):
            _mark(current, line.get("number"), line.get("hits"))


def _jacoco(file: Path, covered: Coverage) -> None:
    for package in ET.parse(file).getroot().iter("package"):
        for source in package.iter("sourcefile"):
            current = covered.setdefault(f"{package.get('name')}/{source.get('name')}", {})
            for line in source.iter("line"):
                _mark(current, line.get("nr"), line.get("ci"))


PARSERS = {"lcov": _lcov, "cobertura": _cobertura, "jacoco": _jacoco}


def _changed_in(report: Coverage, changed: dict[str, frozenset[int]]) -> dict[str, dict[int, bool]]:
    """Returns the changed lines the report knows, by changed file, with whether each is covered.

    A report names a file by an absolute path, a path from some source root or a package, so a changed
    file matches the report file that ends with its path.
    """
    found: dict[str, dict[int, bool]] = {}
    for path, lines in changed.items():
        for name, covered in report.items():
            name = name.replace("\\", "/")
            if name == path or name.endswith("/" + path):
                known = {number: covered[number] for number in sorted(lines) if number in covered}
                if known:
                    found.setdefault(path, {}).update(known)
    return found


def _share(lines: int, covered: int, minimum: float | None) -> dict[str, Any]:
    """Sums up one coverage measure, where no lines to cover count as fully covered."""
    share: dict[str, Any] = {
        "lines": lines,
        "covered": covered,
        "percent": round(100.0 if lines == 0 else 100.0 * covered / lines, 1),
    }
    return share if minimum is None else {**share, "min": minimum}


def _coverage(with_: dict[str, Any], probe: ProbeResult) -> Evaluation:
    """Passes when coverage reaches `min_changed` on the changed lines and `min_total` on the whole report.

    A timeout, a missing or unreadable report and a non-zero exit code all fail.
    The exit code is the source Gate's job when the Gate reads another's Probe, so then the caller passes 0.
    Changed lines that no report line names, such as comments, do not count, and with none left they pass.
    """
    files = sorted(path for path in probe.gate_out.glob(with_["report"]) if path.is_file())
    report: Coverage = {}
    unreadable = ""
    try:
        for file in files:
            PARSERS[with_["format"]](file, report)
    except (ET.ParseError, ValueError) as error:
        unreadable = f"{with_['report']} is not a {with_['format']} report: {error}"
    found = _changed_in(report, probe.changed)
    changed = _share(
        sum(len(lines) for lines in found.values()),
        sum(hit for lines in found.values() for hit in lines.values()),
        with_["min_changed"],
    )
    total = _share(
        sum(len(lines) for lines in report.values()),
        sum(hit for lines in report.values() for hit in lines.values()),
        with_["min_total"],
    )
    reasons = []
    if probe.timed_out:
        reasons.append(f"timed out after {with_['timeout']:g}s")
    elif unreadable:
        reasons.append(unreadable)
    elif not files:
        reasons.append(f"no {with_['format']} report at $GATE_OUT/{with_['report']}")
    else:
        if "min" in changed and changed["percent"] < changed["min"]:
            reasons.append(
                f"{changed['percent']:.1f}% of {changed['lines']} changed lines covered, below {changed['min']:g}%"
            )
        if "min" in total and total["percent"] < total["min"]:
            reasons.append(
                f"{total['percent']:.1f}% of {total['lines']} lines covered in total, below {total['min']:g}%"
            )
        if not reasons and probe.exit_code != 0:
            reasons.append(f"exit code {probe.exit_code} though coverage meets its thresholds")
    uncovered = {path: [n for n, hit in lines.items() if not hit] for path, lines in found.items()}
    uncovered = {path: lines[:MAX_UNCOVERED] for path, lines in uncovered.items() if lines}
    summary: dict[str, Any] = {
        "changed": changed,
        "total": total,
        "uncovered": dict(list(uncovered.items())[:MAX_FAILED]),
    }
    if reasons:
        lines = _lines(probe.log)
        summary["tail"] = lines[len(lines) - TESTS_TAIL :]
    return Evaluation(passed=not reasons, reasons=reasons, summary=summary)


def _coverage_check(with_: dict[str, Any]) -> list[str]:
    if with_["min_changed"] is None and with_["min_total"] is None:
        return ["missing min_changed or min_total"]
    return []


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
    "coverage": Kind(
        fields={
            "run": Field(_optional_command, ""),
            "format": Field(choice(FORMATS)),
            "report": Field(_report_path),
            "min_changed": Field(_percent, None),
            "min_total": Field(_percent, None),
            "timeout": Field(duration, "30m"),
        },
        probe=_command_probe,
        evaluate=_coverage,
        check=_coverage_check,
        evaluates_changed_lines=True,
        reads_from=True,
    ),
}
