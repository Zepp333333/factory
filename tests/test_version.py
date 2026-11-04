import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

from factory.pipeline.fingerprint import factory_version
from harness import Harness

REPO = Path(__file__).parent.parent
PYPROJECT = REPO / "pyproject.toml"


def test_version_prints_the_installed_package_version_and_help_lists_it(factory: Harness) -> None:
    expected = tomllib.loads(PYPROJECT.read_text())["project"]["version"]

    result = factory.cli("--version")
    help_result = factory.cli("--help")

    assert result.code == 0, result.err
    assert result.out == f"factory {expected}\n"
    assert help_result.code == 0, help_result.err
    assert "--version" in help_result.out


@pytest.mark.parametrize(
    ("git", "suffix"),
    [
        pytest.param(
            f'case "$*" in *--show-toplevel*) echo "{REPO}" ;; *) echo a1f3c9d ;; esac', "+a1f3c9d", id="its-own"
        ),
        pytest.param("echo /another/checkout", "", id="another-checkout"),
        pytest.param("echo 'fatal: not a git repository' >&2; exit 128", "", id="no-checkout"),
        pytest.param(None, "", id="no-git"),
    ],
)
def test_the_factory_version_names_a_commit_only_when_the_factory_runs_from_its_own_checkout(
    git: str | None,
    suffix: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    if git is not None:
        fake = tmp_path / "git"
        fake.write_text(f"#!/bin/sh\n{git}\n")
        fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    factory_version.cache_clear()
    request.addfinalizer(factory_version.cache_clear)

    assert factory_version() == version("software-factory") + suffix
