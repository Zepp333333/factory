"""The Seam 2 tests judge Probes with the `tests` kind as a pure function of its `with` and the JUnit reports."""

from pathlib import Path
from typing import Any

import pytest

from factory.gates import ProbeResult, judge, resolve_with

FIXTURES = Path(__file__).parent / "fixtures" / "gates"


def probe(directory: Path, exit_code: int | None = 0, timed_out: bool = False) -> ProbeResult:
    return ProbeResult(
        gate_out=directory / "gate_out",
        log=directory / "log.txt",
        exit_code=exit_code,
        timed_out=timed_out,
        duration=240.0,
    )


def tests(**fields: Any) -> dict[str, Any]:
    resolved, errors = resolve_with(
        "tests", {"run": 'make test REPORT="$GATE_OUT/junit.xml"', "junit": "junit.xml", **fields}
    )
    assert errors == []
    return resolved


def report_dir(tmp_path: Path, *reports: tuple[str, str]) -> Path:
    (tmp_path / "gate_out").mkdir()
    (tmp_path / "log.txt").write_text("log line\n")
    for name, text in reports:
        (tmp_path / "gate_out" / name).write_text(text)
    return tmp_path


def test_green_report_and_zero_exit_code_pass() -> None:
    verdict = judge("tests", "tests", tests(), probe(FIXTURES / "tests-green"))

    assert verdict == {
        "gate": "tests",
        "kind": "tests",
        "outcome": "pass",
        "reasons": [],
        "summary": {"total": 2, "failed": []},
        "log": str(FIXTURES / "tests-green" / "log.txt"),
        "exit_code": 0,
        "duration": 240.0,
    }


def test_red_report_fails_with_each_failed_test_its_message_and_place() -> None:
    verdict = judge("tests", "tests", tests(), probe(FIXTURES / "tests-fail", exit_code=8))

    assert verdict["outcome"] == "fail"
    assert verdict["reasons"] == ["2 of 4 tests failed"]
    assert verdict["summary"] == {
        "total": 4,
        "failed": [
            {
                "test": "hrv::layer3_tests",
                "message": "REQUIRE( rmssd(rr) == Approx(42.1) ) with expansion: 39.8 == Approx(42.1)",
                "where": "tests/layer3/hrv_test.cpp:57",
            },
            {
                "test": "sleep_stages::layer5_tests",
                "message": "SIGSEGV - Segmentation violation signal",
                "where": "tests/layer5/sleep_test.cpp:130",
            },
        ],
        "tail": ["layer3_tests ... Failed", "50% tests passed, 2 tests failed out of 4"],
    }


def test_missing_report_fails_even_with_a_zero_exit_code() -> None:
    verdict = judge("tests", "tests", tests(), probe(FIXTURES / "tests-no-report", exit_code=0))

    assert verdict["outcome"] == "fail"
    assert verdict["reasons"] == ["no JUnit report at $GATE_OUT/junit.xml"]
    assert verdict["summary"] == {"total": 0, "failed": [], "tail": ["make: *** [test] Error 2"]}


def test_crash_after_a_green_report_fails_on_the_exit_code() -> None:
    verdict = judge("tests", "tests", tests(), probe(FIXTURES / "tests-crash", exit_code=139))

    assert verdict["outcome"] == "fail"
    assert verdict["reasons"] == ["exit code 139 but the report has no failed tests"]
    assert verdict["summary"]["tail"] == ["Segmentation fault (core dumped)"]


def test_timeout_fails_even_with_a_green_report() -> None:
    run = probe(FIXTURES / "tests-green", exit_code=None, timed_out=True)

    verdict = judge("tests", "tests", tests(timeout="30m"), run)

    assert verdict["outcome"] == "fail"
    assert verdict["reasons"] == ["timed out after 1800s"]


def test_report_without_tests_fails(tmp_path: Path) -> None:
    run = probe(report_dir(tmp_path, ("junit.xml", "<testsuites/>")))

    verdict = judge("tests", "tests", tests(), run)

    assert (verdict["outcome"], verdict["reasons"]) == ("fail", ["the report has no tests"])


def test_report_that_is_not_xml_fails(tmp_path: Path) -> None:
    run = probe(report_dir(tmp_path, ("junit.xml", "Segmentation fault")))

    verdict = judge("tests", "tests", tests(), run)

    assert verdict["outcome"] == "fail"
    assert verdict["reasons"][0].startswith("junit.xml is not a JUnit report: ")


def test_glob_joins_the_reports_of_several_files(tmp_path: Path) -> None:
    case = '<testsuite><testcase name="{0}" classname="C">{1}</testcase></testsuite>'
    run = probe(
        report_dir(
            tmp_path,
            ("a.xml", case.format("one", "")),
            ("b.xml", case.format("two", '<failure message="boom">C.kt:3</failure>')),
        )
    )

    verdict = judge("tests", "tests", tests(junit="*.xml"), run)

    assert verdict["reasons"] == ["1 of 2 tests failed"]
    assert verdict["summary"]["failed"] == [{"test": "C::two", "message": "boom", "where": "C.kt:3"}]


def test_summary_stays_bounded_on_a_huge_report(tmp_path: Path) -> None:
    cases = "".join(
        f'<testcase name="t{n}" classname="C"><failure message="{"m" * 5000}">{"w" * 5000}</failure></testcase>'
        for n in range(100)
    )
    run = probe(report_dir(tmp_path, ("junit.xml", f"<testsuite>{cases}</testsuite>")), exit_code=1)

    summary = judge("tests", "tests", tests(), run)["summary"]

    assert summary["total"] == 100
    assert len(summary["failed"]) == 20
    assert all(len(f["message"]) <= 300 and len(f["where"]) <= 200 for f in summary["failed"])


def test_with_gets_defaults_for_the_fields_it_leaves_out() -> None:
    resolved, errors = resolve_with("tests", {"run": "make test > $GATE_OUT/x", "junit": "x"})

    assert errors == []
    assert resolved == {"run": "make test > $GATE_OUT/x", "junit": "x", "timeout": 1800.0}


@pytest.mark.parametrize(
    ("raw", "errors"),
    [
        ({}, [("", "missing run"), ("", "missing junit")]),
        ({"run": "make", "junit": "j.xml"}, [("run", "must write its report to $GATE_OUT")]),
        ({"run": "x $GATE_OUT", "junit": "j.xml", "retries": 3}, [("retries", "unknown field")]),
        ({"run": "x $GATE_OUT", "junit": 5}, [("junit", "must be a string")]),
        ({"run": "x $GATE_OUT", "junit": "/tmp/j.xml"}, [("junit", "must be a path inside $GATE_OUT")]),
        ({"run": "x $GATE_OUT", "junit": "../j.xml"}, [("junit", "must be a path inside $GATE_OUT")]),
    ],
)
def test_with_that_breaks_the_schema_gives_errors(raw: dict[str, Any], errors: list[tuple[str, str]]) -> None:
    assert resolve_with("tests", raw)[1] == errors
