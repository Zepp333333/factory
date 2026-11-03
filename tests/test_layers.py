"""The module graph of `src/factory` has no cycle, keeps the layers of `dependencies.md` and the ceilings of Ce."""

import ast
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import factory

ROOT = Path(factory.__file__).parent

# The packages that each package may import, besides itself.
# A module of a package belongs to the package's group, and a module at the top of `src/factory` is a group of its own.
MAY_IMPORT: dict[str, tuple[str, ...]] = {
    "core": (),
    "pipeline": ("core",),
    "ports": ("core", "pipeline"),
    "adapters": ("core", "pipeline", "ports"),
    "pipeline_loader": ("core", "pipeline", "ports"),
    "config": ("core", "pipeline", "ports", "pipeline_loader"),
    "journal": ("core", "pipeline", "ports"),
    "views": ("core", "pipeline", "ports", "journal"),
    "run": ("core", "pipeline", "ports", "pipeline_loader", "config", "journal"),
    "wiring": ("core", "pipeline", "ports", "adapters", "pipeline_loader", "config", "journal", "views", "run"),
    "render": ("core", "pipeline", "ports", "pipeline_loader", "config", "journal", "views", "run", "wiring"),
    "cli": ("core", "pipeline", "ports", "pipeline_loader", "config", "journal", "views", "run", "wiring"),
}

# The packages that count as one module of the graph, because their modules are one unit.
ONE_MODULE = {"cli"}

# The most modules of `src/factory` that one module may import (Ce), so a hub cannot grow back.
MAX_CE = 20

# The most modules outside it that each package may import, as `make measure-coupling` counted them after #249,
# and `adapters` one more after #260, whose every adapter makes its Kind of `ports/kind`.
# Lower a ceiling when a package sheds an import, and raise it only with the reason in the PR.
PACKAGE_CE = {"core": 0, "pipeline": 5, "ports": 5, "adapters": 13, "journal": 10, "run": 30, "views": 10}

# The most import edges of the whole graph, and the most edges per module (mean Ce), as `make measure-coupling`
# counted them after #250, so coupling that spreads over many modules cannot grow unnoticed.
# #260 added the five edges of the adapters that make their Kind of `ports/kind`, past what it shed,
# and the new module lowered the mean Ce.
# #262 added the module `pull_request_folder`, whose two edges from `wiring` and to `tracker` are one past the edge
# from `wiring` to `artifacts` that it shed, and the new module lowered the mean Ce.
# Lower a ceiling when the graph sheds edges, and raise it only with the reason in the PR.
MAX_EDGES = 450
MAX_MEAN_CE = 6.16

Graph = dict[str, set[str]]


@dataclass(frozen=True)
class Module:
    """A Module of the graph: its group and the files it is read from."""

    group: str
    paths: tuple[Path, ...]


@cache
def tree() -> dict[str, Module]:
    """Returns every module of `src/factory` by name, with the group its place in the tree gives it."""
    files: dict[str, list[Path]] = {}
    for path in sorted(ROOT.rglob("*.py")):
        parts = path.relative_to(ROOT).with_suffix("").parts
        if parts[-1] == "__init__" and parts[0] not in ONE_MODULE:
            continue
        name = parts[0] if parts[0] in ONE_MODULE else parts[-1]
        assert name in ONE_MODULE or name not in files, f"two modules are named {name}: {files.get(name)} and {path}"
        files.setdefault(name, []).append(path)
    return {
        name: Module(paths[0].relative_to(ROOT).parts[0].removesuffix(".py"), tuple(paths))
        for name, paths in files.items()
    }


def dotted_names() -> dict[str, str]:
    """Returns the name of each module of the graph by each dotted path that imports it."""
    found: dict[str, str] = {}
    for name, module in tree().items():
        for path in module.paths:
            parts = path.relative_to(ROOT).with_suffix("").parts
            found[".".join(("factory", *parts)).removesuffix(".__init__")] = name
    return found


def path_of(name: str) -> Path:
    """Returns the file of a module that is one file."""
    (path,) = tree()[name].paths
    return path


def imports_of(source: str, modules: dict[str, str]) -> set[str]:
    """Returns the modules that `source` imports, wherever in the file the import sits.

    `modules` names each module by the dotted paths that import it.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found |= {modules[f"{node.module}.{a.name}"] for a in node.names if f"{node.module}.{a.name}" in modules}
            if node.module in modules:
                found.add(modules[node.module])
        elif isinstance(node, ast.Import):
            found |= {modules[a.name] for a in node.names if a.name in modules}
    return found


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


def violations(graph: Graph, group_of: dict[str, str]) -> list[str]:
    """Returns each import that breaks a layer rule, and each module whose group has no row in MAY_IMPORT."""
    problems: list[str] = []
    for module, targets in sorted(graph.items()):
        if group_of[module] not in MAY_IMPORT:
            problems.append(f"{module} is in {group_of[module]}, which has no row in MAY_IMPORT")
            continue
        for target in sorted(targets):
            source_group, target_group = group_of[module], group_of[target]
            if target_group != source_group and target_group not in MAY_IMPORT[source_group]:
                problems.append(f"{module} ({source_group}) imports {target} ({target_group})")
    return problems


def above_ceiling(graph: Graph, group_of: dict[str, str]) -> list[str]:
    """Returns each module that imports more than MAX_CE modules, and each package above its ceiling in PACKAGE_CE."""
    problems = [
        f"{module} imports {len(targets)} modules, above the ceiling of {MAX_CE}"
        for module, targets in sorted(graph.items())
        if len(targets) > MAX_CE
    ]
    for package, ceiling in sorted(PACKAGE_CE.items()):
        outside = {
            target
            for module, targets in graph.items()
            if group_of[module] == package
            for target in targets
            if group_of[target] != package
        }
        if len(outside) > ceiling:
            problems.append(f"{package} imports {len(outside)} modules from outside it, above its ceiling of {ceiling}")
    return problems


def above_total_ceiling(graph: Graph) -> list[str]:
    """Returns the count of import edges if it is above MAX_EDGES, and the mean Ce if it is above MAX_MEAN_CE."""
    edges = sum(len(targets) for targets in graph.values())
    problems = [f"the graph has {edges} import edges, above the ceiling of {MAX_EDGES}"] if edges > MAX_EDGES else []
    if round(edges / len(graph), 2) > MAX_MEAN_CE:
        problems.append(f"the mean Ce of the graph is {edges / len(graph):.2f}, above the ceiling of {MAX_MEAN_CE}")
    return problems


def groups() -> dict[str, str]:
    """Returns the group of each module of the graph."""
    return {name: module.group for name, module in tree().items()}


def package_graph() -> Graph:
    """Returns a fresh copy of the module graph, which a test may change."""
    return {name: set(targets) for name, targets in _scanned_graph().items()}


@cache
def _scanned_graph() -> Graph:
    modules = dotted_names()
    return {
        name: set().union(*(imports_of(path.read_text(), modules) for path in module.paths)) - {name}
        for name, module in tree().items()
    }


def test_modules_import_no_cycle() -> None:
    assert cycles(package_graph()) == []


def test_modules_import_only_the_groups_their_group_may() -> None:
    assert violations(package_graph(), groups()) == []


def test_only_wiring_may_import_the_adapters() -> None:
    assert [group for group, may in MAY_IMPORT.items() if "adapters" in may] == ["wiring"]


def test_every_group_of_the_rules_is_a_package_or_a_module_of_the_tree() -> None:
    assert sorted(MAY_IMPORT) == sorted(set(groups().values()))


def test_a_module_takes_its_group_from_its_package() -> None:
    assert {
        name: groups()[name] for name in ("events", "sandbox", "local_sandbox", "orchestrator", "config", "cli")
    } == {
        "events": "core",
        "sandbox": "ports",
        "local_sandbox": "adapters",
        "orchestrator": "run",
        "config": "config",
        "cli": "cli",
    }


def test_lazy_and_type_checking_imports_count() -> None:
    source = (
        "if TYPE_CHECKING:\n    from factory import a\n"
        "def f():\n    from factory.p.b import x\n"
        "import factory.p.c\n"
        "from factory.p import d\n"
    )
    modules = {"factory.a": "a", "factory.p.b": "b", "factory.p.c": "c", "factory.p.d": "d", "factory.p.e": "e"}
    assert imports_of(source, modules) == {"a", "b", "c", "d"}


def test_cycle_check_finds_a_ring_but_not_a_diamond() -> None:
    assert cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}}) == [["a", "b", "c"]]
    assert cycles({"a": {"b", "c"}, "b": {"d"}, "c": {"d"}, "d": set()}) == []


def test_layer_check_names_an_import_against_the_rules_and_a_module_in_no_group() -> None:
    graph = package_graph()
    graph["journal"] = graph["journal"] | {"run_entry"}
    graph["newcomer"] = set()

    assert violations(graph, {**groups(), "newcomer": "elsewhere"}) == [
        "journal (journal) imports run_entry (run)",
        "newcomer is in elsewhere, which has no row in MAY_IMPORT",
    ]


def test_no_module_and_no_package_imports_more_modules_than_its_ceiling() -> None:
    assert above_ceiling(package_graph(), groups()) == []


def test_every_package_has_a_ceiling() -> None:
    packages = {path.parent.relative_to(ROOT).parts[0] for path in ROOT.glob("*/__init__.py")}
    assert sorted(PACKAGE_CE) == sorted(packages - ONE_MODULE)


def test_ceiling_check_names_a_module_with_ce_21_and_a_package_above_its_ceiling() -> None:
    graph = package_graph()
    graph["newcomer"] = set(sorted(name for name, group in groups().items() if group == "core")[:10])
    graph["newcomer"] |= set(sorted(name for name, group in groups().items() if group == "run")[:11])
    graph["live"] = graph["live"] | {"library_lookup"}

    assert above_ceiling(graph, {**groups(), "newcomer": "elsewhere"}) == [
        f"newcomer imports 21 modules, above the ceiling of {MAX_CE}",
        f"run imports {PACKAGE_CE['run'] + 1} modules from outside it, above its ceiling of {PACKAGE_CE['run']}",
    ]


def test_run_state_imports_no_port_that_a_live_handle_comes_from() -> None:
    assert package_graph()["run_state"] & {"executor", "sandbox", "workbench"} == set()


def test_no_module_of_the_cli_package_imports_more_than_half_of_src_factory() -> None:
    modules = dotted_names()
    sizes = {path.name: len(imports_of(path.read_text(), modules) - {"cli"}) for path in tree()["cli"].paths}
    assert {name: size for name, size in sizes.items() if size > len(tree()) / 2} == {}


def test_cli_is_a_package_with_a_module_per_command() -> None:
    names = {path.stem for path in tree()["cli"].paths}
    assert names >= {
        "check", "run", "status", "stats", "decide", "cancel", "override", "limits", "resume", "finish", "clean",
        "attach", "common",
    }  # fmt: skip


def test_the_readers_of_the_journal_name_no_event() -> None:
    named = {
        module: sorted(
            {
                node.attr
                for node in ast.walk(ast.parse(path_of(module).read_text()))
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "Event"
            }
        )
        for module in ("run_state", "timeline", "metrics", "timeline_memory", "metrics_memory")
    }
    assert named == {"run_state": [], "timeline": [], "metrics": [], "timeline_memory": [], "metrics_memory": []}


def test_cli_enters_the_engine_only_through_run_entry() -> None:
    engine = {name for name, group in groups().items() if group == "run"}
    assert package_graph()["cli"] & engine == {"run_entry"}


# Each phase of a Run with the class that does its work, as the Orchestrator builds them.
PHASES = {
    "ci_fix": "CiFixer",
    "ci_wait": "CiWaiter",
    "decision": "Decisions",
    "inbox": "Inbox",
    "issue_filing": "IssueFiling",
    "landing": "Lander",
    "moves": "Moves",
    "operations": "Operations",
    "orchestrator": "Orchestrator",
    "probes": "Probes",
    "run_agents": "RunAgents",
    "run_decision": "RunDecisions",
    "run_start": "RunStart",
}


# The phases that other phases use, which the Orchestrator builds once and hands to each phase that uses them.
SHARED = {"Moves", "Decisions", "Operations", "Inbox"}


def phase_classes() -> dict[str, ast.ClassDef]:
    """Returns the class of each phase by its module."""
    return {
        module: node
        for module, name in PHASES.items()
        for node in ast.walk(ast.parse(path_of(module).read_text()))
        if isinstance(node, ast.ClassDef) and node.name == name
    }


def init_of(phase: ast.ClassDef) -> ast.FunctionDef:
    (init,) = [node for node in phase.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"]
    return init


def test_every_phase_takes_the_run_context_first_and_only_the_orchestrator_takes_the_run_tools() -> None:
    takes = {
        module: [
            (arg.arg, ast.unparse(arg.annotation) if arg.annotation else "")
            for arg in init_of(phase).args.args[1:] + init_of(phase).args.kwonlyargs
        ]
        for module, phase in phase_classes().items()
    }
    assert {
        module: args
        for module, args in takes.items()
        if args[0] != ("context", "RunContext")
        or (module != "orchestrator" and any("RunTools" in annotation for _, annotation in args))
    } == {}
    assert takes["orchestrator"] == [("context", "RunContext"), ("tools", "RunTools")]


def test_every_phase_uses_each_tool_it_takes() -> None:
    """A tool that a phase keeps from its constructor is read outside it, so the phase keeps no tool it does not use.

    The Orchestrator takes the tools whole and hands them on, so the check leaves it out.
    """
    unused: dict[str, list[str]] = {}
    for module, phase in phase_classes().items():
        if module == "orchestrator":
            continue
        init = init_of(phase)
        kept = {
            target.attr
            for node in ast.walk(init)
            if isinstance(node, ast.Assign | ast.AnnAssign)
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self"
        }
        read = {
            node.attr
            for method in phase.body
            if isinstance(method, ast.FunctionDef) and method is not init
            for node in ast.walk(method)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"
        }
        if kept - read:
            unused[module] = sorted(kept - read)
    assert unused == {}


def called(call: ast.Call) -> str:
    """The name a call calls, as `Moves` in `Moves(...)` and in `moves.Moves(...)`."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def test_each_shared_phase_is_built_in_the_orchestrator_alone() -> None:
    built = {
        name: sorted(
            module
            for module, found in tree().items()
            for path in found.paths
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call) and called(node) == name
        )
        for name in SHARED
    }
    assert built == {name: ["orchestrator"] for name in SHARED}


def test_the_graph_holds_no_more_import_edges_and_no_higher_mean_ce_than_its_ceilings() -> None:
    assert above_total_ceiling(package_graph()) == []


def test_total_ceiling_check_names_one_edge_more_and_a_higher_mean_ce() -> None:
    graph = package_graph()
    graph["live"] = graph["live"] | {"library_lookup"}
    roots = {name: targets for name, targets in package_graph().items() if targets}

    assert above_total_ceiling(graph) == [
        f"the graph has {MAX_EDGES + 1} import edges, above the ceiling of {MAX_EDGES}",
        f"the mean Ce of the graph is {(MAX_EDGES + 1) / len(graph):.2f}, above the ceiling of {MAX_MEAN_CE}",
    ]
    assert above_total_ceiling({**roots, "leaf": set()}) == [
        f"the mean Ce of the graph is {MAX_EDGES / (len(roots) + 1):.2f}, above the ceiling of {MAX_MEAN_CE}"
    ]
