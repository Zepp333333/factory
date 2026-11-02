"""Maps every branch of `src/factory` to the tests that take it, from a coverage run with one context per test.

`make measure-coverage` records the run with `--cov-context=test`, so each arc it took carries the tests that took it.
A branch is an arc out of a line with more than one way out, as coverage.py counts it for branch coverage.
It writes a JSON file with the number of branches each test takes and the branches that only one test takes.
It runs inside the coverage tools' environment, because it reads the coverage data through coverage.py.
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from coverage import Coverage

REPO = Path(__file__).resolve().parents[2]


def branch_id(path: str, arc: tuple[int, int]) -> str:
    start, end = arc
    return f"{Path(path).relative_to(REPO)}:{start}->{end if end > 0 else 'exit'}"


def main(data_file: Path, out: Path) -> None:
    cov = Coverage(data_file=str(data_file))
    cov.load()
    data = cov.get_data()
    branches: dict[str, set[tuple[int, int]]] = {}
    for path in data.measured_files():
        analysis = cov._analyze(path)
        lines = {line for line, exits in analysis.exit_counts.items() if exits > 1} - analysis.no_branch
        branches[path] = {arc for arc in analysis.arc_possibilities if arc[0] in lines}

    takers: dict[str, set[str]] = defaultdict(set)
    for context in sorted(data.measured_contexts()):
        test = context.rsplit("|", 1)[0]
        if not test:
            continue
        data.set_query_contexts([f"^{re.escape(context)}$"])
        for path, possible in branches.items():
            for arc in set(data.arcs(path) or []) & possible:
                takers[branch_id(path, arc)].add(test)

    taken: dict[str, int] = defaultdict(int)
    only: dict[str, list[str]] = defaultdict(list)
    for branch, tests in takers.items():
        for test in tests:
            taken[test] += 1
        if len(tests) == 1:
            only[next(iter(tests))].append(branch)
    result = {
        "branches": sum(len(arcs) for arcs in branches.values()),
        "taken": len(takers),
        "taken_by_one_test": sum(len(b) for b in only.values()),
        "taken_per_test": dict(sorted(taken.items())),
        "only_taken_by": {test: sorted(b) for test, b in sorted(only.items())},
    }
    out.write_text(json.dumps(result, indent=1) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
