"""Maps every branch of `src/factory` to the tests that take it, from a coverage run with one context per test.

`make measure-coverage` records the run with `--cov-context=test`, so each arc it took carries the tests that took it.
A pytest worker labels its data `<test>|<phase>`, and a process a test spawned labels it `<test> (<phase>)`.
A branch is an arc out of a line with more than one way out, as coverage.py counts it for branch coverage.
It writes a JSON file with the tests that take each branch, the number of branches each test takes,
and the branches that only one test takes.
It runs inside the coverage tools' environment, because it reads the coverage data through coverage.py.
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from coverage import Coverage
from coverage.results import analysis_from_file_reporter

REPO = Path(__file__).resolve().parents[2]


def branch_id(path: str, arc: tuple[int, int]) -> str:
    start, end = arc
    return f"{Path(path).relative_to(REPO)}:{start}->{end if end > 0 else 'exit'}"


def test_of(context: str) -> str:
    return re.sub(r" \((setup|call|teardown)\)$", "", context.rsplit("|", 1)[0])


def main(data_file: Path, out: Path) -> None:
    cov = Coverage(data_file=str(data_file))
    cov.load()
    data = cov.get_data()
    branches: dict[str, set[tuple[int, int]]] = {}
    reporters = {}
    for path in data.measured_files():
        # coverage.py has no public call for the file reporter, so this relies on the pinned version.
        reporters[path] = cov._get_file_reporter(path)
        analysis = analysis_from_file_reporter(data, 2, reporters[path], path)
        lines = {line for line, exits in analysis.exit_counts.items() if exits > 1} - analysis.no_branch
        branches[path] = {arc for arc in analysis.arc_possibilities if arc[0] in lines}

    every = {branch_id(path, arc) for path, arcs in branches.items() for arc in arcs}
    takers: dict[str, set[str]] = {branch: set() for branch in every}
    outside: set[str] = set()
    running: set[str] = set()
    for context in sorted(data.measured_contexts()):
        test = test_of(context)
        if test:
            running.add(test)
        data.set_query_contexts([f"^{re.escape(context)}$"])
        for path, possible in branches.items():
            # The analysis maps recorded arcs to the lines the report uses, as `coverage report` does.
            for arc in analysis_from_file_reporter(data, 2, reporters[path], path).arcs_executed_set & possible:
                if test:
                    takers[branch_id(path, arc)].add(test)
                else:
                    outside.add(branch_id(path, arc))

    taken: dict[str, int] = defaultdict(int)
    only: dict[str, list[str]] = defaultdict(list)
    for branch, tests in takers.items():
        for test in tests:
            taken[test] += 1
        if len(tests) == 1:
            only[next(iter(tests))].append(branch)
    result = {
        "branches": len(every),
        "taken": sum(1 for tests in takers.values() if tests),
        "taken_only_outside_tests": sorted(b for b in outside if not takers[b]),
        "taken_by_one_test": sum(len(b) for b in only.values()),
        "tests_running_code": sorted(running),
        "taken_per_test": dict(sorted(taken.items())),
        "only_taken_by": {test: sorted(b) for test, b in sorted(only.items())},
        "takers": {branch: sorted(tests) for branch, tests in sorted(takers.items())},
    }
    out.write_text(json.dumps(result, indent=1) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
