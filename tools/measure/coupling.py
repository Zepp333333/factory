"""Measures the coupling and cohesion of `src/factory` and prints a Markdown report.

It reads two sources:

- the import graph, parsed from every module with `ast`, lazy and `TYPE_CHECKING` imports included,
  with the `cli` package counted as one module as `tests/test_layers.py` does;
- the git history of `src/factory`, for how many modules one commit touches and which modules change together.

For each module it prints Ca (modules that import it), Ce (modules it imports), the names that cross each way,
the instability I = Ce / (Ca + Ce), and the cohesion of its top-level definitions as the count of connected
components of the graph in which two definitions are joined when one names the other.
A module with many components is a bag of unrelated definitions, and a module with one is one unit.

Usage: `python tools/measure/coupling.py [--commits N]`, from the repository root.
"""

import argparse
import ast
import itertools
import statistics
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "factory"


def module_name(path: Path) -> str | None:
    """Names a module by its file name, wherever in the packages it sits, and the `cli` package by `cli`.

    The `__init__` of a package, which only says what the package holds, is no module of the graph.
    """
    parts = path.relative_to(SRC).with_suffix("").parts
    if parts[0] == "cli":
        return "cli"
    if parts[-1] == "__init__":
        return "__init__" if len(parts) == 1 else None
    return parts[-1]


def dotted_name(path: Path) -> str:
    """Returns the dotted path that imports the file at `path`."""
    return ".".join(("factory", *path.relative_to(SRC).with_suffix("").parts)).removesuffix(".__init__")


def read_modules() -> tuple[dict[str, list[ast.Module]], dict[str, str]]:
    """Returns the parsed files of each module, and the name of each module by each dotted path that imports it."""
    modules: dict[str, list[ast.Module]] = defaultdict(list)
    dotted: dict[str, str] = {}
    for path in sorted(SRC.rglob("*.py")):
        if (name := module_name(path)) is not None:
            modules[name].append(ast.parse(path.read_text()))
            dotted[dotted_name(path)] = name
    return modules, dotted


def import_edges(modules: dict[str, list[ast.Module]], dotted: dict[str, str]) -> dict[str, dict[str, set[str]]]:
    """Returns, for each module, the modules it imports and the names it takes from each."""
    edges: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for mod, trees in modules.items():
        for tree in trees:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    if (target := dotted.get(node.module)) and target != mod:
                        edges[mod][target].update(alias.name for alias in node.names)
                    for alias in node.names:
                        if (target := dotted.get(f"{node.module}.{alias.name}")) and target != mod:
                            edges[mod][target].add("<module>")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if (target := dotted.get(alias.name)) and target != mod:
                            edges[mod][target].add("<module>")
    return edges


def cohesion(tree: ast.Module) -> tuple[int, int, int]:
    """Returns the top-level definitions of a module, their connected components and the singletons among them."""
    defs: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs[node.name] = node
        elif isinstance(node, ast.Assign):
            defs.update({t.id: node for t in node.targets if isinstance(t, ast.Name)})
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defs[node.target.id] = node
    adjacent: dict[str, set[str]] = {name: set() for name in defs}
    for name, node in defs.items():
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in defs and sub.id != name:
                adjacent[name].add(sub.id)
                adjacent[sub.id].add(name)
    seen: set[str] = set()
    sizes: list[int] = []
    for name in defs:
        if name in seen:
            continue
        stack, size = [name], 0
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            size += 1
            stack.extend(adjacent[current] - seen)
        sizes.append(size)
    return len(defs), len(sizes), sizes.count(1)


def report_graph(modules: dict[str, list[ast.Module]], dotted: dict[str, str]) -> None:
    edges = import_edges(modules, dotted)
    importers: dict[str, set[str]] = defaultdict(set)
    for source, targets in edges.items():
        for target in targets:
            importers[target].add(source)
    rows = []
    for mod, trees in modules.items():
        ca, ce = len(importers[mod]), len(edges[mod])
        names_in = sum(len(edges[x][mod]) for x in importers[mod])
        names_out = sum(len(names) for names in edges[mod].values())
        instability = f"{ce / (ca + ce):.2f}" if ca + ce else "-"
        defs, components, singletons = cohesion(trees[0]) if mod != "cli" else (0, 0, 0)
        loc = sum(len(ast.unparse(tree).splitlines()) for tree in trees)
        rows.append((ca + ce, mod, ca, ce, names_in, names_out, instability, defs, components, singletons, loc))
    count = len(modules)
    edge_count = sum(len(targets) for targets in edges.values())
    print("## Import graph\n")
    density = edge_count / (count * (count - 1))
    print(f"modules: {count}, edges: {edge_count}, density: {density:.3f}, mean Ce: {edge_count / count:.1f}\n")
    print("| module | Ca | Ce | names in | names out | I | defs | components | singletons | loc |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for row in sorted(rows, reverse=True):
        print("| " + " | ".join(str(cell) for cell in row[1:]) + " |")
    print("\n## Widest imports\n")
    widest = sorted(((len(names), a, b, names) for a, t in edges.items() for b, names in t.items()), reverse=True)
    for width, a, b, names in widest[:20]:
        print(f"- `{a}` takes {width} names from `{b}`: {', '.join(sorted(names))}")


def report_history(commits: int) -> None:
    log = subprocess.run(
        ["git", "log", "--no-merges", "--name-only", "--pretty=format:@@%h", "-n", str(commits), "--", "src/factory"],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=True,
    ).stdout
    touched: list[set[str]] = []
    for line in log.splitlines():
        if line.startswith("@@"):
            touched.append(set())
        elif line.startswith("src/factory/") and line.endswith(".py") and (name := module_name(REPO / line)):
            touched[-1].add(name)
    touched = [files for files in touched if files]
    sizes = sorted(len(files) for files in touched)
    print(f"\n## Co-change over the last {len(touched)} commits that touch `src/factory`\n")
    p90 = sizes[int(len(sizes) * 0.9)]
    median, mean = statistics.median(sizes), statistics.mean(sizes)
    print(f"modules per commit: median {median}, mean {mean:.1f}, p90 {p90}, max {sizes[-1]}\n")
    counts: Counter[str] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()
    for files in touched:
        counts.update(files)
        pairs.update(itertools.combinations(sorted(files), 2))
    print("| module | commits |")
    print("|---|---|")
    for name, count in counts.most_common(15):
        print(f"| {name} | {count} |")
    print("\n| pair | together | Jaccard |")
    print("|---|---|---|")
    for (a, b), count in pairs.most_common(20):
        jaccard = count / (counts[a] + counts[b] - count)
        print(f"| {a} + {b} | {count} | {jaccard:.2f} |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--commits", type=int, default=400, help="how many commits of history to read")
    args = parser.parse_args()
    report_graph(*read_modules())
    report_history(args.commits)


if __name__ == "__main__":
    main()
