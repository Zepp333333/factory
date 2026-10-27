"""The Seam 2 tests judge Probes with the `coverage` kind as a pure function of its `with` and the reports."""

from pathlib import Path
from typing import Any

import pytest

from factory.gates import ProbeResult, judge, resolve_with

FIXTURES = Path(__file__).parent / "fixtures" / "gates"

# Lines 2 to 4 of the covered file changed, and line 3 is the one the reports show uncovered.
FORMATS = {
    "lcov": ("coverage-lcov", "lcov.info", "src/app.py"),
    "cobertura": ("coverage-cobertura", "coverage.xml", "src/app.py"),
    "jacoco": ("coverage-jacoco", "jacoco.xml", "com/acme/App.java"),
}


def with_of_coverage(format: str, **fields: Any) -> dict[str, Any]:
    _, report, _ = FORMATS[format]
    resolved, errors = resolve_with(
        "coverage", {"run": 'make cov OUT="$GATE_OUT"', "format": format, "report": report, "threshold": 60, **fields}
    )
    assert errors == []
    return resolved


def probe(
    directory: Path, changed: dict[str, frozenset[int]], exit_code: int | None = 0, timed_out: bool = False
) -> ProbeResult:
    return ProbeResult(
        gate_out=directory / "gate_out",
        log=directory / "log.txt",
        exit_code=exit_code,
        timed_out=timed_out,
        duration=12.0,
        changed=changed,
    )


@pytest.mark.parametrize("format", FORMATS)
def test_changed_lines_below_the_threshold_fail_with_the_uncovered_ones(format: str) -> None:
    directory, _, path = FORMATS[format]
    changed = {path: frozenset({2, 3, 4}), "README.md": frozenset({1})}

    verdict = judge("cov", "coverage", with_of_coverage(format, threshold=80), probe(FIXTURES / directory, changed))

    assert verdict["outcome"] == "fail"
    assert verdict["reasons"] == ["66.7% of 3 changed lines covered, below 80%"]
    assert verdict["summary"] == {
        "changed": 3,
        "covered": 2,
        "percent": 66.7,
        "threshold": 80.0,
        "uncovered": {path: [3]},
        "tail": ["log line"],
    }


@pytest.mark.parametrize("format", FORMATS)
def test_changed_lines_at_the_threshold_pass(format: str) -> None:
    directory, _, path = FORMATS[format]

    verdict = judge(
        "cov",
        "coverage",
        with_of_coverage(format, threshold=66),
        probe(FIXTURES / directory, {path: frozenset({2, 3, 4})}),
    )

    assert (verdict["outcome"], verdict["reasons"]) == ("pass", [])
    assert verdict["summary"]["percent"] == 66.7


@pytest.mark.parametrize("format", FORMATS)
def test_uncovered_lines_nobody_changed_do_not_count(format: str) -> None:
    directory, _, path = FORMATS[format]

    verdict = judge(
        "cov",
        "coverage",
        with_of_coverage(format, threshold=100),
        probe(FIXTURES / directory, {path: frozenset({1, 2})}),
    )

    assert verdict["outcome"] == "pass"


def test_changed_lines_the_report_does_not_name_leave_nothing_to_cover() -> None:
    verdict = judge(
        "cov",
        "coverage",
        with_of_coverage("lcov", threshold=100),
        probe(FIXTURES / "coverage-lcov", {"docs/a.md": frozenset({1})}),
    )

    assert (verdict["outcome"], verdict["summary"]["changed"]) == ("pass", 0)


@pytest.mark.parametrize("format", FORMATS)
def test_missing_report_fails(format: str) -> None:
    verdict = judge(
        "cov",
        "coverage",
        with_of_coverage(format),
        probe(FIXTURES / "coverage-no-report", {"src/app.py": frozenset({1})}),
    )

    assert verdict["outcome"] == "fail"
    assert verdict["reasons"] == [f"no {format} report at $GATE_OUT/{FORMATS[format][1]}"]


def test_report_in_another_format_fails_as_unreadable() -> None:
    verdict = judge(
        "cov",
        "coverage",
        with_of_coverage("jacoco", report="lcov.info"),
        probe(FIXTURES / "coverage-lcov", {"a": frozenset({1})}),
    )

    assert verdict["outcome"] == "fail"
    assert verdict["reasons"][0].startswith("lcov.info is not a jacoco report")


def test_timeout_and_non_zero_exit_with_covered_lines_fail() -> None:
    changed = {"src/app.py": frozenset({1, 2})}
    directory = FIXTURES / "coverage-lcov"

    timed_out = judge("cov", "coverage", with_of_coverage("lcov", timeout="2s"), probe(directory, changed, None, True))
    crashed = judge("cov", "coverage", with_of_coverage("lcov"), probe(directory, changed, exit_code=2))

    assert timed_out["reasons"] == ["timed out after 2s"]
    assert crashed["reasons"] == ["exit code 2 but the changed lines are covered"]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"format": "gcov", "report": "a", "threshold": 50}, "format"),
        ({"format": "lcov", "report": "a", "threshold": 101}, "threshold"),
        ({"format": "lcov", "report": "/etc/a", "threshold": 50}, "report"),
        ({"format": "lcov", "report": "a"}, "missing threshold"),
        ({"format": "lcov", "report": "a", "threshold": 50, "run": "make"}, "run"),
    ],
)
def test_with_is_checked_against_the_schema(raw: dict[str, Any], message: str) -> None:
    _, errors = resolve_with("coverage", raw)

    assert any(message in " ".join(error) for error in errors)
