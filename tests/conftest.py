import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness import Harness

# Contract tests drive a real `sbx`, `claude`, Orca and GitHub, which take minutes and wait on their own deadlines.
CONTRACT = Path(__file__).with_name("contract")
CONTRACT_TIMEOUT = 600


def pytest_configure(config: pytest.Config) -> None:
    put_real_git_first()


def put_real_git_first() -> None:
    """Puts git's own binary first on the PATH when the `git` on it is a wrapper, such as the xcrun shim of macOS.

    The shim looks up the real git on every call, which takes longer than most calls themselves.
    """
    found = shutil.which("git")
    exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True, check=True).stdout.strip()
    real = Path(exec_path) / "git"
    if found and real.is_file() and not os.path.samefile(found, real):
        os.environ["PATH"] = f"{real.parent}{os.pathsep}{os.environ['PATH']}"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.is_relative_to(CONTRACT):
            item.add_marker(pytest.mark.timeout(CONTRACT_TIMEOUT))


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[Harness]:
    """Gives the test a Harness, and fails the test when it leaves a process running that a correct Run would end.

    A test whose Run dies on purpose calls `Harness.crashed`, so the processes of the dead Run may stay.
    """
    harness = Harness(tmp_path)
    yield harness
    left = harness.end()
    assert not left, "the test left processes running:\n" + "\n".join(f"{pid} {cmd}" for pid, cmd in left.items())
