from pathlib import Path

import pytest

from harness import Harness

# Contract tests drive a real `sbx`, `claude`, Orca and GitHub, which take minutes and wait on their own deadlines.
CONTRACT = Path(__file__).with_name("contract")
CONTRACT_TIMEOUT = 600


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.is_relative_to(CONTRACT):
            item.add_marker(pytest.mark.timeout(CONTRACT_TIMEOUT))


@pytest.fixture
def factory(tmp_path: Path) -> Harness:
    return Harness(tmp_path)
