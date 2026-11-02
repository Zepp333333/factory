"""The Seam 1 tests run `coverage` Gates, with and without `from`, through `factory run` and `factory check`."""

import json
from pathlib import Path
from typing import Any

from harness import AgentScript, Harness, run_id_of, ticket_text, writes

TICKET = "---\ntitle: App\n---\nWrite app.py.\n"
APP = AgentScript(files={"app.py": "a = 1\nb = 2\nc = 3\n"}, output={"summary": "Add app.py", "handoff": "done"})
LCOV = 'printf \'SF:%s/app.py\\nDA:1,1\\nDA:2,0\\nDA:3,1\\nend_of_record\\n\' "$PWD" > "$GATE_OUT/lcov.info"'


def with_coverage(factory: Harness, min_changed: float, source: bool = True) -> None:
    """Puts a `tests` Gate that writes an lcov report and a `coverage` Gate after it, with `fail` abandoning."""
    factory.pipeline["stages"]["implement"]["next"]["done"] = "tests"
    coverage: dict[str, Any] = {
        "kind": "coverage",
        "with": {"format": "lcov", "report": "lcov.info", "min_changed": min_changed},
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


def test_gate_with_from_judges_the_source_probe_and_a_decision_after_it_reads_the_verdict_and_routes_the_ticket(
    factory: Harness,
) -> None:
    """Puts the risk Decision after the `coverage` Gates, so one Pipeline holds both a Decision and a Gate with from.

    The Gate `cov` with from runs no Probe of its own and judges the changed lines by the report of the `tests` Probe,
    and the Gate `own` after it runs its own Probe.
    The read-only review Stage leaves a file that `.gitignore` covers, which is a Workspace violation.
    """
    with_coverage(factory, 60)
    factory.pipeline["gates"]["cov"]["next"]["pass"] = "own"
    factory.pipeline["gates"]["own"] = {
        "kind": "coverage",
        "with": {"format": "lcov", "report": "lcov.info", "min_changed": 60, "run": LCOV},
        "next": {"pass": "review", "fail": "abandoned"},
    }
    (factory.config / "prompts" / "reviewer.md").write_text("Review the change.\n")
    (factory.config / "schemas" / "reviewer.json").write_text(json.dumps({"type": "object"}))
    factory.pipeline["roles"]["reviewer"] = {"prompt": "prompts/reviewer.md", "output": "schemas/reviewer.json"}
    factory.pipeline["stages"]["review"] = {
        "role": "reviewer",
        "workspace": "ro",
        "inputs": ["spec", "ticket", {"gate": "cov"}],
        "next": {"done": "risk"},
    }
    factory.pipeline["decisions"] = {
        "risk": {
            "decider": {"kind": "function", "function": "risk"},
            "outcomes": ["risky", "safe"],
            "inputs": [{"stage": "review"}],
            "next": {"safe": "merge", "risky": "abandoned"},
        }
    }
    factory.executor.scripts["implement"] = AgentScript(
        files={**APP.files, ".gitignore": "*.xml\n"}, output={"summary": "Add app.py", "handoff": "done"}
    )
    factory.executor.scripts["review"] = AgentScript(
        files={"report.xml": "<r/>\n"}, output={"door": "two_way", "blast_radius": "local"}
    )

    result = factory.run(factory.write_spec("App", "Write an app.", {"t1": TICKET}))

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [source, cov, own] = verdicts(factory, run_id)
    assert (own["gate"], own["outcome"]) == ("own", "pass")
    assert (source["gate"], source["outcome"]) == ("tests", "pass")
    assert (cov["gate"], cov["outcome"]) == ("cov", "pass")
    assert cov["summary"]["changed"] == {"lines": 3, "covered": 2, "percent": 66.7, "min": 60.0}
    assert cov["log"] == source["log"]
    assert [e["node"] for e in factory.events(run_id, "ProbeStarted")] == ["tests", "own"]
    [_, cov_finished, _] = factory.events(run_id, "GateFinished")
    assert (cov_finished["from"], cov_finished["probe"]) == ("tests", 1)
    cov_dir = factory.run_dir(run_id) / "tickets" / "t1" / "cov"
    assert [p.name for p in cov_dir.iterdir()] == ["from-tests-probe-1"]
    assert Path(cov_finished["verdict"]) == cov_dir / "from-tests-probe-1" / "verdict.json"
    [request] = [r for r in factory.executor.requests if r.stage == "review"]
    [verdict] = [i for i in json.loads(request.brief.read_text())["inputs"] if i["source"] == "gate"]
    assert (verdict["node"], verdict["from"], verdict["probe"]) == ("cov", "tests", 1)
    assert [e["outcome"] for e in factory.events(run_id, "DecisionDecided")] == ["safe"]
    [violation] = factory.events(run_id, "WorkspaceViolated")
    assert violation["removed_ignored"] == ["report.xml"]
    assert factory.git("show", f"factory/{run_id}:app.py").splitlines() == ["a = 1", "b = 2", "c = 3"]


def test_check_refuses_from_on_a_kind_that_cannot_read_another_probe(factory: Harness) -> None:
    with_coverage(factory, 60)
    factory.pipeline["gates"]["tests"]["from"] = "cov"

    assert "gates.tests.from: must name a Gate, and a Gate of kind command has no from" in factory.check().err


def test_min_changed_after_sync_judges_only_the_lines_of_the_ticket(
    factory: Harness,
) -> None:
    """Ticket `a` lands uncovered lines, then `b` syncs them in and its `coverage` Gate must not see them."""
    gate_ran = factory.root / "a-gate-ran"
    report = (
        'for f in *.txt; do echo "SF:$PWD/$f"; for i in 1 2 3; do '
        'echo "DA:$i,$([ $f = a.txt ] && echo 0 || echo 1)"; done; echo end_of_record; done > "$GATE_OUT/lcov.info"; '
        f"touch {gate_ran}"
    )
    factory.project["max_parallel"] = 2
    factory.pipeline["stages"]["implement"]["next"]["done"] = "sync"
    factory.pipeline["operations"]["sync"]["next"]["clean"] = "cov"
    factory.pipeline["gates"] = {
        "cov": {
            "kind": "coverage",
            "with": {"format": "lcov", "report": "lcov.info", "run": report, "min_changed": 100},
            "next": {"pass": "merge", "fail": "merge"},
        }
    }
    factory.executor.ticket_scripts[("a", "implement")] = writes("a")
    factory.executor.ticket_scripts[("b", "implement")] = writes("b", wait_for=gate_ran)
    spec = factory.write_spec("Files", "Two files.", {name: ticket_text(name) for name in "ab"})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    by_ticket = {
        e["ticket"]: json.loads(Path(e["verdict"]).read_text()) for e in factory.events(run_id, "GateFinished")
    }
    assert by_ticket["a"]["outcome"] == "fail"
    assert by_ticket["b"]["outcome"] == "pass"
    assert by_ticket["b"]["summary"]["changed"]["lines"] == 1
