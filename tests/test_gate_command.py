"""The Seam 2 tests evaluate Probes with the `command` kind as a pure function of its `with` and the Probe's reports."""

from pathlib import Path
from typing import Any

import pytest

from factory.gates import ProbeResult, evaluate, resolve_with

FIXTURES = Path(__file__).parent / "fixtures" / "gates"


def probe(directory: Path, exit_code: int | None = 0, timed_out: bool = False) -> ProbeResult:
    return ProbeResult(
        gate_out=directory / "gate_out",
        log=directory / "log.txt",
        exit_code=exit_code,
        timed_out=timed_out,
        duration=95.0,
    )


def command(**fields: Any) -> dict[str, Any]:
    resolved, errors = resolve_with("command", {"run": "make build", **fields})
    assert errors == []
    return resolved


def log_dir(tmp_path: Path, text: str) -> Path:
    (tmp_path / "gate_out").mkdir()
    (tmp_path / "log.txt").write_text(text)
    return tmp_path


def test_failed_build_gives_fail_with_matched_lines_and_the_tail_of_the_log() -> None:
    verdict = evaluate("build", "command", command(tail=3), probe(FIXTURES / "build-fail", exit_code=2))

    assert verdict == {
        "gate": "build",
        "kind": "command",
        "outcome": "fail",
        "reasons": ["exit code 2"],
        "summary": {
            "matched": [
                "/work/src/layer3/hrv.cpp:88:17: error: no member named 'rmssd_window' in 'wcc::HrvConfig'",
                "/work/src/layer3/hrv.cpp:102:5: error: use of undeclared identifier 'clamp_ms'",
            ],
            "tail": [
                "2 errors generated.",
                "make[2]: *** [CMakeFiles/wcc_core.dir/build.make:76: CMakeFiles/wcc_core.dir/src/layer3/hrv.cpp.o]"
                " Error 1",
                "make: *** [Makefile:12: build] Error 2",
            ],
        },
        "log": str(FIXTURES / "build-fail" / "log.txt"),
        "exit_code": 2,
        "duration": 95.0,
    }


def test_zero_exit_code_passes_whatever_the_log_says(tmp_path: Path) -> None:
    verdict = evaluate("build", "command", command(), probe(log_dir(tmp_path, "warning: error-prone code\n")))

    assert (verdict["outcome"], verdict["reasons"]) == ("pass", [])


def test_timeout_fails_even_when_the_killed_command_left_no_code(tmp_path: Path) -> None:
    run = probe(log_dir(tmp_path, "[ 98%] building\n"), exit_code=None, timed_out=True)

    verdict = evaluate("build", "command", command(timeout="20m"), run)

    assert verdict["outcome"] == "fail"
    assert verdict["reasons"] == ["timed out after 1200s"]
    assert verdict["exit_code"] is None


def test_match_picks_the_summary_lines(tmp_path: Path) -> None:
    text = "ok\nFAILED tests/test_a.py::test_one\nok\nFAILED tests/test_b.py::test_two\n"

    verdict = evaluate("tests", "command", command(match="^FAILED", tail=0), probe(log_dir(tmp_path, text), 1))

    assert verdict["summary"] == {
        "matched": ["FAILED tests/test_a.py::test_one", "FAILED tests/test_b.py::test_two"],
        "tail": [],
    }


def test_summary_stays_bounded_on_a_huge_log(tmp_path: Path) -> None:
    text = "".join(f"error {n}: {'x' * 5000}\n" for n in range(500))

    verdict = evaluate("build", "command", command(tail=1000), probe(log_dir(tmp_path, text), 1))

    summary = verdict["summary"]
    assert len(summary["matched"]) == 20
    assert len(summary["tail"]) == 200
    assert all(len(line) <= 500 for line in summary["matched"] + summary["tail"])


def test_log_that_is_not_utf8_is_still_summarized(tmp_path: Path) -> None:
    (tmp_path / "gate_out").mkdir()
    (tmp_path / "log.txt").write_bytes(b"error: \xff\xfe broken\n")

    verdict = evaluate("build", "command", command(), probe(tmp_path, 1))

    assert verdict["summary"]["matched"] == ["error: �� broken"]


def test_with_gets_defaults_for_the_fields_it_leaves_out() -> None:
    assert resolve_with("command", {"run": "make"}) == (
        {"run": "make", "timeout": 1800.0, "match": r"\berror\b", "tail": 40},
        [],
    )


@pytest.mark.parametrize(
    ("raw", "errors"),
    [
        ({}, [("", "missing run")]),
        ({"run": "make", "retries": 3}, [("retries", "unknown field")]),
        ({"run": 5}, [("run", "must be a string")]),
        ({"run": "make", "timeout": "half an hour"}, [("timeout", "must be a duration such as 90s, 45m or 2h")]),
        (
            {"run": "make", "match": "error("},
            [("match", "must be a regular expression: missing ), unterminated subpattern at position 5")],
        ),
        ({"run": "make", "tail": -1}, [("tail", "must be a non-negative integer")]),
        ({"run": "make", "tail": True}, [("tail", "must be a non-negative integer")]),
    ],
)
def test_with_that_breaks_the_schema_gives_errors(raw: dict[str, Any], errors: list[tuple[str, str]]) -> None:
    assert resolve_with("command", raw)[1] == errors
