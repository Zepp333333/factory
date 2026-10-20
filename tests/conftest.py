from pathlib import Path

import pytest

from harness import Harness


@pytest.fixture
def factory(tmp_path: Path) -> Harness:
    return Harness(tmp_path)
