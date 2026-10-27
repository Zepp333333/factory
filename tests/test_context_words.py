import ast
import re
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "factory"

# A word is caught as a whole part of a name, so `release` does not hit `lease`.
# Words with no technical meaning in code, each with the term of `CONTEXT.md`
# that replaces it. Words with a legitimate meaning (`branch` in git, `output`
# and `result` of an agent, `check`, `handoff`, `session`) stay out of the list.
FORBIDDEN = {
    "judgment": "Decision",
    "judge": "Decider",
    "persona": "Role",
    "iteration": "Round",
    "lease": "Slot",
    "coordinator": "Orchestrator",
    "validator": "Gate",
    "summarizer": "Describer",
    "jury": "Consilium",
    "deliverable": "Artifact",
}

# Names that belong to a library, which the engine cannot rename.
LIBRARY_NAMES = {"Draft202012Validator"}


def _names(tree: ast.AST) -> list[tuple[int, str]]:
    """Every name the tree defines or uses, with its line."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            found.append((node.lineno, node.name))
        elif isinstance(node, ast.arg):
            found.append((node.lineno, node.arg))
        elif isinstance(node, ast.Attribute):
            found.append((node.lineno, node.attr))
        elif isinstance(node, ast.Name):
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.keyword) and node.arg is not None:
            found.append((node.value.lineno, node.arg))
    return sorted(found)


def _words(name: str) -> set[str]:
    """The lowercase words of a snake_case or CamelCase name, without a plural `s`."""
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)
    return {w.lower().removesuffix("s") for w in parts}


def forbidden_names(source: str, filename: str) -> list[str]:
    """One message per forbidden word in a name: file, line, name and the term to use."""
    messages = []
    seen = set()
    for line, name in _names(ast.parse(source, filename)):
        for word, term in FORBIDDEN.items():
            if name not in LIBRARY_NAMES and word in _words(name) and (line, name, word) not in seen:
                seen.add((line, name, word))
                messages.append(f"{filename}:{line}: `{name}` contains `{word}`, CONTEXT.md calls it {term}")
    return messages


def test_the_scan_names_file_line_name_and_term_of_a_forbidden_name() -> None:
    source = (
        "class Judgment:\n"
        "    def run(self, persona):\n"
        "        self._model_judgment = 1\n"
        "def judge_all():\n"
        "    iteration = 0\n"
    )

    assert forbidden_names(source, "fixture.py") == [
        "fixture.py:1: `Judgment` contains `judgment`, CONTEXT.md calls it Decision",
        "fixture.py:2: `persona` contains `persona`, CONTEXT.md calls it Role",
        "fixture.py:3: `_model_judgment` contains `judgment`, CONTEXT.md calls it Decision",
        "fixture.py:4: `judge_all` contains `judge`, CONTEXT.md calls it Decider",
        "fixture.py:5: `iteration` contains `iteration`, CONTEXT.md calls it Round",
    ]


def test_the_scan_passes_names_without_forbidden_words() -> None:
    assert forbidden_names("def decide(role, round):\n    slot = 1\n", "ok.py") == []


def test_no_name_in_src_factory_contains_a_forbidden_word() -> None:
    messages = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent.parent)
        messages += forbidden_names(path.read_text(), str(rel))

    assert messages == []
