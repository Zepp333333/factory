"""Guards the rules of `CODING_STANDARDS.md` that keep the suite lean: inventory, private names, time budget."""

import ast
import csv
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

TESTS = Path(__file__).parent
REPO = TESTS.parent
INVENTORY = TESTS / "inventory.csv"


def inventory() -> list[dict[str, str]]:
    with INVENTORY.open(newline="") as rows:
        return list(csv.DictReader(rows))


def collected() -> list[str]:
    """Returns the node id of every test that `pytest` collects from the repository, contract tests included."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return [line for line in result.stdout.splitlines() if "::" in line]


def test_every_collected_test_has_an_inventory_row_with_a_behaviour_and_a_source() -> None:
    rows = {row["test"]: row for row in inventory()}
    tests = collected()

    missing = [test for test in tests if test not in rows]
    unnamed = [test for test in tests if test in rows and not (rows[test]["behaviour"] and rows[test]["source"])]
    stale = sorted(set(rows) - set(tests))
    assert not missing, "tests/inventory.csv has no row for:\n" + "\n".join(missing)
    assert not unnamed, "these rows of tests/inventory.csv name no behaviour or no source:\n" + "\n".join(unnamed)
    assert not stale, "tests/inventory.csv names tests pytest does not collect:\n" + "\n".join(stale)


LEVELS = {"unit", "harness", "contract"}


def test_no_two_tests_share_a_behaviour_and_a_level() -> None:
    rows = inventory()
    unknown = [f"{row['test']}: {row['level']!r}" for row in rows if row["level"] not in LEVELS]
    assert not unknown, f"these rows of tests/inventory.csv name a level outside {sorted(LEVELS)}:\n" + "\n".join(
        unknown
    )
    owners: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        if row["behaviour"]:
            owners[(row["behaviour"], row["level"])].add(row["test"].split("[", 1)[0])

    shared = {key: tests for key, tests in owners.items() if len(tests) > 1}
    assert not shared, "\n".join(
        f"{level} {behaviour!r}: {sorted(tests)}" for (behaviour, level), tests in shared.items()
    )


def private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def in_factory(module: str) -> bool:
    return module == "factory" or module.startswith("factory.")


# The calls that name an attribute by a string, as `getattr(module, "_name")` or `monkeypatch.setattr("a.b._c", x)`.
ATTRIBUTE_CALLS = {"getattr", "setattr", "hasattr", "delattr", "delitem", "patch", "object"}


class PrivateNames(ast.NodeVisitor):
    """Finds every private name of `src/factory` that a test module imports or reaches through a name it imported.

    A private attribute of an object that a public call returned stays out of reach of a scan of the source.
    """

    def __init__(self) -> None:
        self.found: list[tuple[int, str]] = []
        self.bound: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if in_factory(alias.name):
                self.check(node, alias.name.split("."), alias.name)
                self.bound.add(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level or not in_factory(module):
            return
        self.check(node, module.split("."), module)
        for alias in node.names:
            self.check(node, [alias.name], f"{module}.{alias.name}")
            self.bound.add(alias.asname or alias.name)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        chain: list[str] = []
        root: ast.expr = node
        while isinstance(root, ast.Attribute):
            chain.insert(0, root.attr)
            root = root.value
        if isinstance(root, ast.Name) and root.id in self.bound:
            self.check(node, chain, ".".join([root.id, *chain]))
            return
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Catches `getattr`, `setattr` and `monkeypatch.setattr` on an imported name with a private attribute,
        and a dotted target such as `monkeypatch.setattr("factory.attempt._charge", ...)`."""
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        if name not in ATTRIBUTE_CALLS:
            self.generic_visit(node)
            return
        target = node.args[0] if node.args else None
        if isinstance(target, ast.Constant) and isinstance(target.value, str) and in_factory(target.value):
            self.check(node, target.value.split("."), target.value)
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
            root = node.args[0]
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in self.bound:
                self.check(node, [node.args[1].value], f"{root.id}.{node.args[1].value}")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        """Forgets the imported names that a parameter or a local name of the function or lambda shadows."""
        arguments = node.args
        local = {a.arg for a in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]}
        local |= {a.arg for a in (arguments.vararg, arguments.kwarg) if a is not None}
        body = node.body if isinstance(node.body, list) else [node.body]
        local |= {
            n.id for part in body for n in ast.walk(part) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        }
        outer = self.bound
        self.bound = outer - local
        self.generic_visit(node)
        self.bound = outer

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def visit_ListComp(self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp | ast.DictComp) -> None:
        """Forgets the imported names that a target of the comprehension shadows."""
        local = {n.id for gen in node.generators for n in ast.walk(gen.target) if isinstance(n, ast.Name)}
        outer = self.bound
        self.bound = outer - local
        self.generic_visit(node)
        self.bound = outer

    visit_SetComp = visit_GeneratorExp = visit_DictComp = visit_ListComp

    def check(self, node: ast.AST, parts: list[str], name: str) -> None:
        if any(private(part) for part in parts):
            self.found.append((getattr(node, "lineno", 0), name))


def private_names(source: str) -> list[tuple[int, str]]:
    finder = PrivateNames()
    finder.visit(ast.parse(source))
    return finder.found


def test_the_scan_finds_each_way_a_test_reaches_a_private_name() -> None:
    source = (
        "import factory.attempt\n"
        "import factory._hidden as hidden\n"
        "from factory.attempt import _charge, charge\n"
        "from factory import wiring\n"
        "from factory.wiring import Registry\n"
        "wiring._registry\n"
        "Registry._factories\n"
        "factory.attempt._spend()\n"
        "getattr(wiring, '_registry')\n"
        "monkeypatch.setattr('factory.attempt._charge', None)\n"
        "def test(factory):\n"
        "    factory.clock._now\n"
        "def test_local():\n"
        "    factory = harness()\n"
        "    factory.clock._now\n"
        "    [wiring._x for wiring in items]\n"
        "    (lambda wiring: wiring._x)\n"
        "log(wiring, '_label')\n"
        "print('factory._not_a_target')\n"
        "from harness import _helper\n"
    )

    assert private_names(source) == [
        (2, "factory._hidden"),
        (3, "factory.attempt._charge"),
        (6, "wiring._registry"),
        (7, "Registry._factories"),
        (8, "factory.attempt._spend"),
        (9, "wiring._registry"),
        (10, "factory.attempt._charge"),
    ]


def test_no_test_imports_a_private_name_of_the_factory() -> None:
    found = [
        f"{path.relative_to(REPO)}:{line}: {name}"
        for path in sorted(TESTS.rglob("*.py"))
        if TESTS / "fixtures" not in path.parents
        for line, name in private_names(path.read_text())
    ]

    assert not found, "tests reach private names of src/factory, which CODING_STANDARDS.md forbids:\n" + "\n".join(
        found
    )


def pytest_with_budget(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Runs `pytest` with the time budget plugin on a project with one slow test and one slow contract test."""
    (tmp_path / "tests" / "contract").mkdir(parents=True, exist_ok=True)
    slow = "import time\n\ndef test_slow():\n    time.sleep(0.15)\n"
    (tmp_path / "tests" / "test_slow.py").write_text(slow)
    (tmp_path / "tests" / "contract" / "test_contract.py").write_text(slow.replace("0.15", "0.4"))
    env = {**os.environ, "PYTHONPATH": str(TESTS)}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "time_budget", "-p", "no:cacheprovider", "-p", "no:xdist", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )


def test_summed_test_time_past_the_budget_fails_the_run_only_with_the_flag_and_contract_tests_do_not_count(
    tmp_path: Path,
) -> None:
    over = pytest_with_budget(tmp_path, "-o", "time_budget=0.1", "--time-budget")
    under = pytest_with_budget(tmp_path, "-o", "time_budget=0.3", "--time-budget")
    unchecked = pytest_with_budget(tmp_path, "-o", "time_budget=0.1")
    unset = pytest_with_budget(tmp_path, "--time-budget")

    assert over.returncode == 1, over.stdout
    assert "over the budget of 0.1 s" in over.stdout
    assert under.returncode == 0, under.stdout
    assert "of the budget of 0.3 s" in under.stdout
    assert unchecked.returncode == 0, unchecked.stdout
    assert unset.returncode == 4, unset.stdout
    assert "--time-budget needs the ini option time_budget" in unset.stderr


def test_a_database_of_a_test_commits_without_waiting_for_the_disk(tmp_path: Path) -> None:
    db = sqlite3.connect(tmp_path / "journal.db")

    assert db.execute("PRAGMA synchronous").fetchone() == (0,)
    db.close()
