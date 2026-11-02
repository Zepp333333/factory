import tomllib
from pathlib import Path

from harness import Harness

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def test_version_prints_the_installed_package_version_and_help_lists_it(factory: Harness) -> None:
    expected = tomllib.loads(PYPROJECT.read_text())["project"]["version"]

    result = factory.cli("--version")
    help_result = factory.cli("--help")

    assert result.code == 0, result.err
    assert result.out == f"factory {expected}\n"
    assert help_result.code == 0, help_result.err
    assert "--version" in help_result.out
