"""The Seam 1 tests run `coverage` Gates, with and without `from`, through `factory run` and `factory check`."""

import json
from pathlib import Path
from typing import Any

from harness import AgentScript, Harness, run_id_of

TICKET = "---\ntitle: App\n---\nWrite app.py.\n"
APP = AgentScript(files={"app.py": "a = 1\nb = 2\nc = 3\n"}, output={"summary": "Add app.py", "handoff": "done"})
LCOV = 'printf \'SF:%s/app.py\\nDA:1,1\\nDA:2,0\\nDA:3,1\\nend_of_record\\n\' "$PWD" > "$GATE_OUT/lcov.info"'


def with_coverage(factory: Harness, threshold: float, source: bool = True) -> None:
    """Puts a `tests` Gate that writes an lcov report and a `coverage` Gate after it, with `fail` abandoning."""
    factory.pipeline["stages"]["implement"]["next"]["done"] = "tests"
    coverage: dict[str, Any] = {
        "kind": "coverage",
        "with": {"format": "lcov", "report": "lcov.info", "threshold": threshold},
        "next": {"pass": "merge", "fail": "abandoned"},
    }
    if source:
        coverage["from"] = "tests"
    else:
        coverage["with"]["run"] = LCOV
    factory.pipeline["gates"] = {
        "tests": {"kind": "command", "with": {"run": LCOV}, "next": {"pass": "cov", "fail": "abandoned"}},
        "cov": coverage,
    }


def verdicts(factory: Harness, run_id: str) -> list[dict[str, Any]]:
    return [json.loads(Path(e["verdict"]).read_text()) for e in factory.events(run_id, "GateFinished")]


def test_gate_with_from_judges_the_changed_lines_by_the_source_probe(factory: Harness) -> None:
    with_coverage(factory, 60)
    factory.executor.scripts["implement"] = APP

    result = factory.run(factory.write_spec("App", "Write an app.", {"t1": TICKET}))

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [source, cov] = verdicts(factory, run_id)
    assert (source["gate"], source["outcome"]) == ("tests", "pass")
    assert (cov["gate"], cov["outcome"], cov["summary"]["percent"], cov["summary"]["covered"]) == (
        "cov",
        "pass",
        66.7,
        2,
    )
    assert cov["log"] == source["log"]


def test_changed_lines_below_the_threshold_fail_the_gate(factory: Harness) -> None:
    with_coverage(factory, 90)
    factory.executor.scripts["implement"] = APP

    result = factory.run(factory.write_spec("App", "Write an app.", {"t1": TICKET}))

    assert result.code == 1
    [_, cov] = verdicts(factory, run_id_of(result.out))
    assert cov["outcome"] == "fail"
    assert cov["reasons"] == ["66.7% of 3 changed lines covered, below 90%"]
    assert cov["summary"]["uncovered"] == {"app.py": [2]}


def test_gate_with_its_own_probe_runs_it(factory: Harness) -> None:
    with_coverage(factory, 60, source=False)
    factory.pipeline["gates"]["tests"]["next"]["pass"] = "cov"
    factory.executor.scripts["implement"] = APP

    result = factory.run(factory.write_spec("App", "Write an app.", {"t1": TICKET}))

    assert result.code == 0, result.out
    assert [v["outcome"] for v in verdicts(factory, run_id_of(result.out))] == ["pass", "pass"]


def test_check_refuses_a_path_that_skips_the_source_gate(factory: Harness) -> None:
    with_coverage(factory, 60)
    factory.pipeline["stages"]["implement"]["next"]["done"] = "cov"

    result = factory.check()

    assert result.code != 0
    assert "gates.cov.from: a path reaches cov without passing through tests" in result.err


def test_check_refuses_a_source_that_is_not_a_probe_gate(factory: Harness) -> None:
    with_coverage(factory, 60)
    factory.pipeline["gates"]["cov"]["from"] = "missing"

    result = factory.check()

    assert result.code != 0
    assert "gates.cov.from: missing is not a Gate that runs a Probe" in result.err


def test_check_refuses_run_with_from_and_neither_run_nor_from(factory: Harness) -> None:
    with_coverage(factory, 60)
    factory.pipeline["gates"]["cov"]["with"]["run"] = LCOV
    assert "run is the Probe's, and a Gate with from has none" in factory.check().err

    with_coverage(factory, 60, source=False)
    del factory.pipeline["gates"]["cov"]["with"]["run"]
    assert "missing run, or from" in factory.check().err
