"""The CI guard judges the diff of a CI fixer's Round, so the check never rests on the agent's word.

A fix may not touch the CI config, delete a test or add a marker that skips or expects a failure.
The guard is pure: it reads the changed files and the patch of `git diff -U0`, and returns why the diff is refused.
"""

import re

TEST_DIRECTORIES = {"tests", "test", "__tests__", "spec"}
TEST_FILE = re.compile(r"(^|/)(test_[^/]*|[^/]*_test\.[^/]*|[^/]*\.(test|spec)\.[^/]*|conftest\.py)$")

# A line that defines a test, in the languages of the projects the factory builds.
TEST_DEFINITION = re.compile(
    r"^\s*(?:async\s+)?(?:def\s+(test\w*)|func\s+(Test\w*)|fn\s+(test\w*)|(?:it|test)\s*\(\s*['\"`]([^'\"`]*)"
    r"|(?:public\s+)?void\s+(test\w*)|TEST(?:_F)?\s*\(\s*([\w\s,]+))"
)

# A line that makes a test skip or expect a failure.
SKIP_MARKER = re.compile(
    r"pytest\.mark\.(skip|skipif|xfail)|pytest\.(skip|xfail)\(|unittest\.(skip|skipIf|skipUnless|expectedFailure)"
    r"|\b(?:it|test|describe)\.(?:skip|todo|failing)\b|\b(?:xit|xtest|xdescribe)\s*\(|\bt\.Skip(?:f|Now)?\("
    r"|#\[ignore|@Ignore\b|@Disabled\b|GTEST_SKIP|\bDISABLED_\w+"
)


def is_test_path(path: str) -> bool:
    """Tells whether a path is a test file or sits in a test directory."""
    return bool(TEST_FILE.search(path)) or any(part in TEST_DIRECTORIES for part in path.split("/")[:-1])


def problems(files: list[tuple[str, str]], patch: str, ci_config: tuple[str, ...]) -> list[str]:
    """Returns why a fix's diff is refused, which is nothing for a diff that passes.

    `files` lists the status letter and path of each changed file, `patch` is `git diff -U0` of the change,
    and `ci_config` the paths of the CI config that the code host of the Run's PullRequests reads.
    A removed test definition counts as deleted unless the diff adds one of the same name, and a deleted file's
    removed lines are not counted again, because its `+++` line names no path.
    """
    found = [f"{path} is CI config" for _, path in files if path.startswith(ci_config)]
    found += [f"{path} is a deleted test file" for status, path in files if status == "D" and is_test_path(path)]
    removed: dict[str, str] = {}
    added: set[str] = set()
    path = ""
    for line in patch.splitlines():
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/")
            continue
        if not (line.startswith("+") or line.startswith("-")) or not is_test_path(path):
            continue
        text = line[1:]
        if line.startswith("-"):
            if (name := _test_name(text)) is not None:
                removed.setdefault(f"{path}::{name}", name)
            continue
        if (name := _test_name(text)) is not None:
            added.add(name)
        if SKIP_MARKER.search(text):
            found.append(f"{path} adds a skip or xfail marker: {text.strip()}")
    found += [f"{key.split('::')[0]} deletes test {name}" for key, name in removed.items() if name not in added]
    return found


def _test_name(text: str) -> str | None:
    match = TEST_DEFINITION.match(text)
    if match is None:
        return None
    return next(group for group in match.groups() if group is not None).strip()
