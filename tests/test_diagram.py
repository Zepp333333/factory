"""`factory diagram` draws a Pipeline or a Run graph, and `factory check` holds the block on top of its file to it."""

import shutil
from pathlib import Path

import pytest
import yaml

from factory.config import display_path
from harness import FACTORY_LIBRARY, Harness

REPO = FACTORY_LIBRARY.parents[2]
SNAPSHOTS = Path(__file__).with_name("fixtures") / "diagrams"
SHIPPED = sorted(
    path
    for root in (FACTORY_LIBRARY, REPO / ".factory")
    for kind in ("pipelines", "runs")
    for path in (root / kind).glob("*.yaml")
)
BEGIN = "# factory-diagram: begin"
END = "# factory-diagram: end"
DRAWN = {
    "start": "build",
    "escalate_to": "ended",
    "nodes": {
        "select": {"kind": "filter", "with": {"label": "ready"}, "next": {"selected": "build", "empty": "ended"}},
        "build": {
            "kind": "tickets",
            "with": {"pipeline": {"file": "a#b.yaml"}},
            "next": {"all_merged": "end", "some_merged": "end", "none_merged": "ended"},
        },
        "end": {"role": "describer", "next": {"done": "open"}},
        "open": {"kind": "open_pr", "next": {"opened": "wait"}},
        "wait": {
            "kind": "wait_tracker",
            "with": {"on": ["approved"], "timeout": "3d"},
            "next": {"approved": "fix", "timeout": "ask"},
        },
        "fix": {
            "role": "ci-fixer",
            "workspace": "integration",
            "rounds": {"max": 2, "then": "ask"},
            "next": {"done": "gate", "failed": "fix"},
        },
        "gate": {"kind": "command", "with": {"run": "make"}, "next": {"pass": "ended", "fail": "fix"}},
        "ask": {
            "decider": {"kind": "human"},
            "outcomes": {"again": "Go on", "stop": "Stop"},
            "next": {"again": {"to": "fix", "reset_rounds": ["fix"]}, "stop": "ended"},
        },
    },
}


def graph_file(factory: Harness, name: str, data: object, text_above: str = "") -> Path:
    path = factory.repo / ".factory" / "runs" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text_above + yaml.safe_dump(data, sort_keys=False))
    return path


def diagram(factory: Harness, *argv: str) -> str:
    result = factory.cli("diagram", *argv, "--project", str(factory.project_file))
    assert result.code == 0, result.err
    return result.out


def errors_of(factory: Harness, *argv: str) -> list[str]:
    result = factory.cli(*argv, "--project", str(factory.project_file))
    assert result.code == 2, result.out + result.err
    return [line.removeprefix("factory: ") for line in result.err.splitlines()]


@pytest.mark.parametrize(
    ("snapshot", "target"),
    [
        ("self-pipeline", ("self", "--kind", "pipeline")),
        ("self-run", ("self", "--kind", "run")),
        ("default", ("default",)),
        ("review-via-tracker", ("review-via-tracker",)),
        ("approve-then-merge", ("approve-then-merge",)),
    ],
)
def test_diagram_prints_a_shipped_graph_as_its_snapshot(
    factory: Harness, snapshot: str, target: tuple[str, ...]
) -> None:
    shutil.copytree(FACTORY_LIBRARY / "pipelines", factory.factory_library / "pipelines")
    shutil.copytree(REPO / ".factory" / "pipelines", factory.repo / ".factory" / "pipelines")
    shutil.copytree(REPO / ".factory" / "runs", factory.repo / ".factory" / "runs")

    assert diagram(factory, *target) == (SNAPSHOTS / f"{snapshot}.mmd").read_text()


@pytest.mark.parametrize("path", SHIPPED, ids=lambda path: str(path.relative_to(REPO)))
def test_a_shipped_graph_starts_with_the_block_of_the_flowchart_it_prints(factory: Harness, path: Path) -> None:
    printed = diagram(factory, str(path))

    assert printed.startswith("flowchart TD\n")
    comment = "".join(f"# {line}\n" for line in printed.splitlines())
    first, _, rest = path.read_text().partition("\n")
    assert first.startswith(BEGIN)
    assert rest.startswith(f"{comment}{END}\n")


def test_each_kind_of_node_has_its_shape_and_each_edge_its_outcome(factory: Harness) -> None:
    drawn = diagram(factory, str(graph_file(factory, "drawn", DRAWN))).splitlines()

    assert drawn[0] == "flowchart TD"
    assert drawn[1:] == [
        '    _start(["start<br/>escalate_to: ended"])',
        '    select[/"select<br/>filter"/]',
        '    build[["build<br/>tickets: a#35;b.yaml"]]',
        '    end_["end<br/>role: describer"]',
        '    open[/"open<br/>open_pr"/]',
        '    wait(("wait<br/>wait_tracker<br/>timeout: 3d"))',
        '    fix["fix<br/>role: ci-fixer<br/>rounds: max 2"]',
        '    gate{{"gate<br/>command"}}',
        '    ask{"ask<br/>human"}',
        '    ended(["ended"])',
        "    _start --> build",
        '    select -->|"selected"| build',
        '    select -->|"empty"| ended',
        '    build -->|"all_merged"| end_',
        '    build -->|"some_merged"| end_',
        '    build -->|"none_merged"| ended',
        '    end_ -->|"done"| open',
        '    end_ -.->|"failed"| ended',
        '    open -->|"opened"| wait',
        '    open -.->|"failed"| ended',
        '    wait -->|"approved"| fix',
        '    wait -->|"timeout"| ask',
        '    fix -->|"done"| gate',
        '    fix -->|"failed"| fix',
        '    fix -->|"exhausted"| ask',
        '    gate -->|"pass"| ended',
        '    gate -->|"fail"| fix',
        '    ask -->|"again, reset_rounds: fix"| fix',
        '    ask -->|"stop"| ended',
        '    ask -.->|"escalate"| ended',
    ]


def test_a_pipeline_draws_its_sections_and_sends_an_unrouted_escalation_to_escalate_to(factory: Harness) -> None:
    path = factory.repo / "pipeline.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "start": "work",
                "escalate_to": "abandoned",
                "stages": {"work": {"role": "implementer", "rounds": {"max": 3}, "next": {"done": "file"}}},
                "operations": {
                    "file": {"kind": "file_issues", "next": {"filed": "tell"}},
                    "tell": {"kind": "comment", "on_fail": "escalate", "next": {"done": "done"}},
                },
            },
            sort_keys=False,
        )
    )

    assert diagram(factory, str(path)).splitlines()[1:] == [
        '    _start(["start<br/>escalate_to: abandoned"])',
        '    work["work<br/>role: implementer<br/>rounds: max 3"]',
        '    file[/"file<br/>file_issues"/]',
        '    tell[/"tell<br/>comment"/]',
        '    abandoned(["abandoned"])',
        '    done(["done"])',
        "    _start --> work",
        '    work -->|"done"| file',
        '    work -.->|"exhausted"| abandoned',
        '    work -.->|"failed"| abandoned',
        '    file -->|"filed"| tell',
        '    file -.->|"refused"| abandoned',
        '    file -.->|"failed"| abandoned',
        '    tell -->|"done"| done',
        '    tell -.->|"failed"| abandoned',
    ]


def test_a_name_resolves_through_the_layers_and_a_name_of_both_kinds_needs_its_kind(factory: Harness) -> None:
    shipped = diagram(factory, "default")
    assert "tickets: ${vars.pipeline}" in shipped

    graph_file(factory, "default", DRAWN)
    assert 'wait(("wait<br/>wait_tracker<br/>timeout: 3d"))' in diagram(factory, "default")

    pipelines = factory.repo / ".factory" / "pipelines"
    pipelines.mkdir()
    (pipelines / "default.yaml").write_text(yaml.safe_dump({"start": "a", "stages": {"a": {"role": "r"}}}))
    assert errors_of(factory, "diagram", "default") == [
        "default is both a Pipeline and a Run graph of the Library; name one with --kind pipeline or --kind run"
    ]
    assert '    a["a<br/>role: r"]' in diagram(factory, "default", "--kind", "pipeline").splitlines()

    assert errors_of(factory, "diagram", "nope") == ["nope is no file and no Pipeline or Run graph of the Library"]


def test_a_file_that_is_no_yaml_mapping_gets_no_diagram(factory: Harness) -> None:
    broken = factory.repo / "broken.yaml"
    broken.write_text("- a\n")

    assert errors_of(factory, "diagram", str(broken)) == [f"{display_path(broken)} must hold a YAML mapping"]
    assert errors_of(factory, "diagram", "--write", str(broken)) == [f"{display_path(broken)} must hold a YAML mapping"]


def test_write_puts_the_block_on_top_and_replaces_it_on_the_next_write(factory: Harness) -> None:
    path = graph_file(factory, "mine", DRAWN, "# Why this graph exists.\n")
    original = path.read_text()

    assert diagram(factory, "--write", str(path)) == f"{display_path(path)}: diagram written\n"
    written = path.read_text()
    printed = diagram(factory, str(path))
    assert written.startswith(f"{BEGIN}")
    assert written.endswith(f"{END}\n\n{original}")
    assert "".join(f"# {line}\n" for line in printed.splitlines()) in written

    diagram(factory, "--write", str(path))
    assert path.read_text() == written

    changed = {**DRAWN, "start": "select"}
    path.write_text(written.replace(original, "# Why this graph exists.\n" + yaml.safe_dump(changed, sort_keys=False)))
    diagram(factory, "--write", str(path))
    assert path.read_text().count(BEGIN) == 1
    assert "    _start --> select\n".replace("    ", "#     ", 1) in path.read_text()
    assert "_start --> build" not in path.read_text()


def test_write_takes_only_a_file(factory: Harness) -> None:
    assert errors_of(factory, "diagram", "--write", "default") == [
        "--write takes a file, and default is none; `factory library show` prints a graph to copy into one"
    ]


def test_write_to_a_file_it_cannot_change_names_the_file(factory: Harness) -> None:
    path = graph_file(factory, "mine", DRAWN)
    path.chmod(0o444)

    errors = errors_of(factory, "diagram", "--write", str(path))

    assert errors[0].startswith(f"cannot write {display_path(path)}: ")


def test_check_fails_on_a_missing_or_stale_block_and_passes_after_write(factory: Harness) -> None:
    graph = yaml.safe_load((FACTORY_LIBRARY / "runs" / "default.yaml").read_text())
    path = graph_file(factory, "mine", graph)
    factory.project["run"] = "mine"
    shown = display_path(path)

    assert errors_of(factory, "check") == [
        f"{shown}: has no diagram block at its top; run `factory diagram --write {shown}`"
    ]

    diagram(factory, "--write", str(path))
    assert factory.check().code == 0

    path.write_text(path.read_text().replace("max: 2", "max: 4"))
    assert errors_of(factory, "check") == [
        f"{shown}: its diagram block differs from the diagram the file produces; run `factory diagram --write {shown}`"
    ]

    diagram(factory, "--write", str(path))
    assert factory.check().code == 0


def test_check_holds_the_pipeline_file_and_a_graph_no_project_names_to_their_blocks(factory: Harness) -> None:
    assert factory.check().code == 0
    pipeline = factory.repo / "mine.yaml"
    pipeline.write_text(yaml.safe_dump({k: v for k, v in factory.pipeline.items() if k != "roles"}))
    factory.project["pipeline"] = {"file": str(pipeline)}
    unused = graph_file(factory, "unused", DRAWN)

    assert errors_of(factory, "check") == [
        f"{display_path(path)}: has no diagram block at its top; run `factory diagram --write {display_path(path)}`"
        for path in (pipeline, unused)
    ]

    diagram(factory, "--write", str(pipeline))
    diagram(factory, "--write", str(unused))
    assert factory.check().code == 0


def test_library_show_prints_a_pipeline_and_the_layer_it_came_from(factory: Harness) -> None:
    shutil.copytree(FACTORY_LIBRARY / "pipelines", factory.factory_library / "pipelines")
    project = ("--project", str(factory.project_file))

    shown = factory.cli("library", "show", "pipeline", "tdd-review", *project)

    assert shown.code == 0, shown.err
    first, _, rest = shown.out.partition("\n")
    assert first == "# Pipeline tdd-review (factory)"
    assert rest == (FACTORY_LIBRARY / "pipelines" / "tdd-review.yaml").read_text()
    assert errors_of(factory, "library", "show", "pipeline", "nope") == ["Pipeline nope is in no Layer"]
