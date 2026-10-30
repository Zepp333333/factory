"""The module graph of `src/factory` has no import cycle and keeps the layers of `dependencies.md`."""

import ast
from pathlib import Path

import factory

# The groups of modules. A module in no group fails the test, so a new module has to pick its group.
GROUPS: dict[str, tuple[str, ...]] = {
    "foundation": ("clock", "events", "front_matter", "git", "names", "spawn", "states", "values"),
    "domain": ("deciders", "fingerprint", "gates", "library_lookup", "pipeline", "pipeline_checks", "throttle"),
    "ports": ("executor", "sandbox", "tracker", "workbench"),
    "adapters": ("claude_code", "github", "gitlab", "sbx", "tracker_cli"),
    "project": ("config",),
    "storage": ("commands", "custody", "journal"),
    "state": (
        "artifacts",
        "metrics",
        "projection",
        "question",
        "recovery",
        "run_state",
        "run_view",
        "stats",
        "timeline",
    ),
    "engine": (
        "attempt",
        "ci_wait",
        "clean",
        "decision",
        "landing",
        "live",
        "merge_window",
        "operations",
        "orchestrator",
        "probes",
        "run_decision",
        "run_start",
        "sleep_block",
    ),
    "wiring": ("render", "wiring"),
    "root": ("cli",),
}

# The groups that each group may import, besides itself.
MAY_IMPORT: dict[str, tuple[str, ...]] = {
    "foundation": (),
    "domain": ("foundation",),
    "ports": ("foundation", "domain"),
    "adapters": ("foundation", "domain", "ports"),
    "project": ("foundation", "domain", "ports"),
    "storage": ("foundation",),
    "state": ("foundation", "domain", "ports", "storage"),
    "engine": ("foundation", "domain", "ports", "project", "storage", "state"),
    "wiring": ("foundation", "domain", "ports", "adapters", "project", "storage", "state", "engine"),
    "root": ("foundation", "domain", "ports", "project", "storage", "state", "engine", "wiring"),
}

# The adapter classes that live in the modules of their port, which only `wiring` may import.
ADAPTER_CLASSES = {"LocalSandbox", "OrcaWorkbench", "TerminalWorkbench"}

Graph = dict[str, set[str]]


def imports_of(source: str, modules: set[str]) -> set[str]:
    """Returns the modules of the package that `source` imports, wherever in the file the import sits."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module == "factory":
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("factory."):
            found.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            found |= {a.name.split(".")[1] for a in node.names if a.name.startswith("factory.")}
    return found & modules


def cycles(graph: Graph) -> list[list[str]]:
    """Returns each group of modules that import each other in a ring, as the ring's modules in order."""
    found: list[list[str]] = []
    done: set[str] = set()

    def visit(module: str, path: list[str]) -> None:
        if module in path:
            found.append(path[path.index(module) :])
            return
        if module in done:
            return
        for target in sorted(graph[module]):
            visit(target, [*path, module])
        done.add(module)

    for module in sorted(graph):
        visit(module, [])
    return found


def violations(graph: Graph) -> list[str]:
    """Returns each import that breaks a layer rule, and each module that is in no group or in two."""
    group_of: dict[str, str] = {}
    problems: list[str] = []
    for group, members in GROUPS.items():
        for member in members:
            if member in group_of:
                problems.append(f"{member} is in both {group_of[member]} and {group}")
            group_of[member] = group
    for module, targets in sorted(graph.items()):
        if module not in group_of:
            problems.append(f"{module} is in no group of GROUPS")
            continue
        for target in sorted(targets):
            if target not in group_of:
                continue
            source_group, target_group = group_of[module], group_of[target]
            if target_group != source_group and target_group not in MAY_IMPORT[source_group]:
                problems.append(f"{module} ({source_group}) imports {target} ({target_group})")
    return problems


def packages() -> list[Path]:
    """Returns the sub-packages of `src/factory`, each of which counts as one module of the graph."""
    return sorted(path.parent for path in Path(factory.__file__).parent.glob("*/__init__.py"))


def package_graph() -> Graph:
    root = Path(factory.__file__).parent
    sources = {path.stem: path.read_text() for path in root.glob("*.py") if path.stem != "__init__"}
    for package in packages():
        sources[package.name] = "\n".join(path.read_text() for path in sorted(package.glob("*.py")))
    return {name: imports_of(source, set(sources)) - {name} for name, source in sources.items()}


def test_modules_import_no_cycle() -> None:
    assert cycles(package_graph()) == []


def test_modules_import_only_the_groups_their_group_may() -> None:
    assert violations(package_graph()) == []


def test_only_wiring_imports_the_adapter_classes_of_the_ports() -> None:
    root = Path(factory.__file__).parent
    importers = set()
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and {a.name for a in node.names} & ADAPTER_CLASSES:
                importers.add(path.stem)
    assert importers == {"wiring"}


def test_every_module_of_a_group_exists() -> None:
    graph = package_graph()
    assert sorted(m for members in GROUPS.values() for m in members) == sorted(graph)


def test_lazy_and_type_checking_imports_count() -> None:
    source = "if TYPE_CHECKING:\n    from factory import a\ndef f():\n    from factory.b import x\n"
    assert imports_of(source, {"a", "b", "c"}) == {"a", "b"}


def test_cycle_check_fails_on_a_ring() -> None:
    assert cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}}) == [["a", "b", "c"]]


def test_cycle_check_passes_on_a_diamond() -> None:
    assert cycles({"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": set()}) == []


def test_layer_check_fails_on_an_import_against_the_rules() -> None:
    graph = package_graph()
    graph["journal"] = graph["journal"] | {"pipeline"}
    assert violations(graph) == ["journal (storage) imports pipeline (domain)"]


def test_layer_check_fails_on_a_module_in_no_group() -> None:
    graph = package_graph()
    graph["newcomer"] = set()
    assert violations(graph) == ["newcomer is in no group of GROUPS"]


def test_run_state_imports_no_port_that_a_live_handle_comes_from() -> None:
    assert package_graph()["run_state"] & {"executor", "sandbox", "workbench"} == set()


def test_no_module_of_the_cli_package_imports_more_than_half_of_src_factory() -> None:
    root = Path(factory.__file__).parent
    modules = {path.stem for path in root.glob("*.py") if path.stem != "__init__"} | {p.name for p in packages()}
    sizes = {path.name: len(imports_of(path.read_text(), modules) - {"cli"}) for path in (root / "cli").glob("*.py")}
    assert {name: size for name, size in sizes.items() if size > len(modules) / 2} == {}


def test_cli_is_a_package_with_a_module_per_command() -> None:
    names = {path.stem for path in (Path(factory.__file__).parent / "cli").glob("*.py")}
    assert names >= {
        "check", "run", "status", "stats", "decide", "cancel", "override", "limits", "resume", "finish", "clean",
        "attach", "common",
    }  # fmt: skip
