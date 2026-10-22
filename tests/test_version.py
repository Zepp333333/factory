import tomllib
from pathlib import Path

from harness import Harness

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def test_version_prints_the_installed_package_version(factory: Harness) -> None:
    expected = tomllib.loads(PYPROJECT.read_text())["project"]["version"]

    result = factory.cli("--version")

    assert result.code == 0, result.err
    assert result.out == f"factory {expected}\n"


def test_help_lists_version(factory: Harness) -> None:
    result = factory.cli("--help")

    assert result.code == 0, result.err
    assert "--version" in result.out
