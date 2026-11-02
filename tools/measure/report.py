"""Writes the test suite baseline and the measured columns of the inventory from the raw measurements.

It reads what the `measure-*` targets of the Makefile leave in the directory given as its argument:

- `junit.xml` from `measure-durations`;
- `coverage.json` and `branches.json` from `measure-coverage`;
- `profile.jsonl` from `measure-profile`.

It writes `docs/test-suite/baseline/` and fills the columns `level`, `only_branches` and `duration`
of `tests/inventory.csv`, and it keeps the columns `behaviour`, `source` and `verdict` of every test already there.
With `--inventory` it fills only those columns of the inventory and leaves the baseline as it was measured.
"""

import argparse
import csv
import json
import shutil
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "docs" / "test-suite" / "baseline"
INVENTORY = REPO / "tests" / "inventory.csv"

COLUMNS = ["test", "behaviour", "source", "level", "only_branches", "duration", "verdict"]
KEPT = ["behaviour", "source", "verdict"]

# A row of `branches.csv` names the tests that take its branch only up to this many.
# The full map names about 265,000 pairs of branch and test, about 25 MB, so it stays in build/measure/branches.json.
NAMED_TAKERS = 10

# The profile categories in the order the baseline lists them, with Python as the rest of the wall time.
CATEGORIES = ["git", "sqlite", "scripted agent", "idle ticks", "fake CLIs", "other subprocesses", "python"]


@dataclass
class Test:
    id: str
    file: str
    line: int
    outcome: str
    seconds: float


def tests_of(junit: Path) -> tuple[list[Test], float]:
    """Returns every test of a junit report written with `junit_family=xunit1`, and the wall time of the session."""
    root = ET.parse(junit).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    assert suite is not None
    tests = []
    for case in suite.iter("testcase"):
        file = case.get("file", "")
        module = file.removesuffix(".py").replace("/", ".")
        classes = case.get("classname", "").removeprefix(module).strip(".")
        parts = [file, *(classes.split(".") if classes else []), case.get("name", "")]
        outcome = "passed"
        for tag in ("failure", "error", "skipped"):
            if case.find(tag) is not None:
                outcome = "failed" if tag in ("failure", "error") else "skipped"
        tests.append(Test("::".join(parts), file, int(case.get("line", "0")), outcome, float(case.get("time", "0"))))
    tests.sort(key=lambda t: (t.file, t.line, t.id))
    return tests, float(suite.get("time", "0"))


def durations(tests: list[Test], wall: float) -> dict[str, Any]:
    outcomes = Counter(t.outcome for t in tests)
    summed = sum(t.seconds for t in tests)
    over = {limit: [t.seconds for t in tests if t.seconds > limit] for limit in (1, 2)}
    return {
        "collected": len(tests),
        "passed": outcomes["passed"],
        "failed": outcomes["failed"],
        "skipped": outcomes["skipped"],
        "summed_seconds": round(summed, 1),
        "session_wall_seconds": round(wall, 1),
        "over_1s": len(over[1]),
        "over_1s_seconds": round(sum(over[1]), 1),
        "over_2s": len(over[2]),
        "slowest": [{"test": t.id, "seconds": t.seconds} for t in sorted(tests, key=lambda t: -t.seconds)[:20]],
    }


def coverage(report: Path) -> tuple[dict[str, Any], list[list[str]]]:
    data = json.loads(report.read_text())
    totals = data["totals"]
    summary = {
        "tool": f"coverage.py {data['meta']['version']}",
        "statements": totals["num_statements"],
        "covered_lines": totals["covered_lines"],
        "line_percent": round(100 * totals["covered_lines"] / totals["num_statements"], 2),
        "branches": totals["num_branches"],
        "covered_branches": totals["covered_branches"],
        "branch_percent": round(100 * totals["covered_branches"] / totals["num_branches"], 2),
    }
    rows = [["file", "statements", "missing_lines", "branches", "missing_branches"]]
    for name, file in sorted(data["files"].items()):
        s = file["summary"]
        rows.append([name, s["num_statements"], s["missing_lines"], s["num_branches"], s["missing_branches"]])
    return summary, rows


def branch_map(branches: dict[str, Any], tests: list[Test]) -> tuple[dict[str, Any], list[list[Any]]]:
    """Counts what the branch map shows, and lists each branch with the tests that take it."""
    takers: dict[str, list[str]] = branches["takers"]
    passed = [t.id for t in tests if t.outcome == "passed"]
    running = set(branches["tests_running_code"])
    summary = {
        "branches_taken_by_tests": branches["taken"],
        "branches_taken_only_outside_tests": len(branches["taken_only_outside_tests"]),
        "branches_taken_by_no_test": sum(1 for t in takers.values() if not t),
        "branches_taken_by_one_test": branches["taken_by_one_test"],
        "tests_alone_taking_a_branch": len(branches["only_taken_by"]),
        "passed_tests_taking_no_branch": sum(1 for test in passed if test not in branches["taken_per_test"]),
        "passed_tests_running_no_line": sum(1 for test in passed if test not in running),
    }
    rows: list[list[Any]] = [["branch", "tests", "taken_by"]]
    for branch, names in takers.items():
        rows.append([branch, len(names), " ".join(names) if len(names) <= NAMED_TAKERS else ""])
    return summary, rows


def profile(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[list[str]]]:
    """Sums the profile of the harness tests and gives each test's seconds per category."""
    table = [["test", "wall", *CATEGORIES]]
    totals: dict[str, float] = defaultdict(float)
    others: dict[str, float] = defaultdict(float)
    calls: dict[str, int] = defaultdict(int)
    harness = [row for row in rows if row["harness"]]
    for row in rows:
        seconds: dict[str, float] = defaultdict(float)
        for name, took in row["seconds"].items():
            category = "other subprocesses" if name.startswith("other subprocesses") else name
            seconds[category] += took
            if row["harness"] and category == "other subprocesses":
                others[name.split(": ", 1)[-1]] += took
        seconds["python"] = max(0.0, row["wall"] - sum(seconds.values()))
        table.append([row["test"], f"{row['wall']:.3f}", *(f"{seconds[c]:.3f}" for c in CATEGORIES)])
        if row["harness"]:
            for category in CATEGORIES:
                totals[category] += seconds[category]
            for name, n in row["calls"].items():
                calls[name] += n
    wall = sum(row["wall"] for row in harness)
    summary = {
        "harness_tests": len(harness),
        "harness_wall_seconds": round(wall, 1),
        "seconds": {c: round(totals[c], 1) for c in CATEGORIES},
        "share_percent": {c: round(100 * totals[c] / wall, 1) if wall else 0.0 for c in CATEGORIES},
        "other_subprocesses_seconds": {k: round(v, 1) for k, v in sorted(others.items(), key=lambda kv: -kv[1])},
        "calls_per_harness_test": {k: round(v / len(harness), 1) for k, v in sorted(calls.items())} if harness else {},
    }
    return summary, table


def write_csv(path: Path, rows: list[list[Any]]) -> None:
    with path.open("w", newline="") as out:
        csv.writer(out, lineterminator="\n").writerows(rows)


def inventory(tests: list[Test], levels: dict[str, str], branches: dict[str, list[str]]) -> None:
    kept: dict[str, dict[str, str]] = {}
    if INVENTORY.is_file():
        with INVENTORY.open(newline="") as existing:
            kept = {row["test"]: row for row in csv.DictReader(existing)}
    rows: list[list[Any]] = [COLUMNS]
    for test in tests:
        old = kept.get(test.id, {})
        row = {
            **{column: old.get(column, "") for column in KEPT},
            "test": test.id,
            "level": levels[test.id],
            "only_branches": " ".join(branches.get(test.id, [])),
            "duration": f"{test.seconds:.2f}",
        }
        rows.append([row[column] for column in COLUMNS])
    write_csv(INVENTORY, rows)


def levels_of(tests: list[Test], rows: list[dict[str, Any]]) -> dict[str, str]:
    harness = {row["test"] for row in rows if row["harness"]}
    return {
        t.id: "contract" if t.id.startswith("tests/contract/") else "harness" if t.id in harness else "unit"
        for t in tests
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("measure", type=Path, help="the directory the measure-* targets wrote")
    parser.add_argument("--inventory", action="store_true", help="write only the inventory and keep the baseline")
    args = parser.parse_args()
    measure: Path = args.measure
    tests, wall = tests_of(measure / "junit.xml")
    branches = json.loads((measure / "branches.json").read_text())
    rows = [json.loads(line) for line in (measure / "profile.jsonl").read_text().splitlines()]
    levels = levels_of(tests, rows)
    inventory(tests, levels, branches["only_taken_by"])
    if args.inventory:
        return

    OUT.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(measure / "junit.xml", OUT / "junit.xml")
    summary: dict[str, Any] = {"durations": durations(tests, wall)}

    summary["coverage"], files = coverage(measure / "coverage.json")
    write_csv(OUT / "coverage.csv", files)
    summary["branch_map"], branch_rows = branch_map(branches, tests)
    write_csv(OUT / "branches.csv", branch_rows)

    summary["profile"], table = profile(rows)
    write_csv(OUT / "profile.csv", table)

    summary["levels"] = dict(sorted(Counter(levels.values()).items()))

    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
