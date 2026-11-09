import os
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from harness import Harness

# Sums the test time and checks it against `time_budget` of pyproject.toml when CI passes `--time-budget`.
pytest_plugins = ["time_budget"]

# Contract tests drive a real `sbx`, `claude`, Orca and GitHub, which take minutes and wait on their own deadlines.
CONTRACT = Path(__file__).with_name("contract")
CONTRACT_TIMEOUT = 600


def pytest_configure(config: pytest.Config) -> None:
    put_real_git_first()
    turn_off_git_maintenance()
    skip_sqlite_fsync()


def turn_off_git_maintenance() -> None:
    """Stops every git the tests run from starting `git maintenance` in the background after a commit.

    The background process holds `.git/objects/maintenance.lock` for a moment, and a test that copies a template
    repository then fails when the lock is gone before `shutil.copytree` reaches it.
    """
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    os.environ[f"GIT_CONFIG_KEY_{count}"] = "maintenance.auto"
    os.environ[f"GIT_CONFIG_VALUE_{count}"] = "false"
    os.environ["GIT_CONFIG_COUNT"] = str(count + 1)


def skip_sqlite_fsync() -> None:
    """Has every SQLite database the tests open commit without waiting for the disk.

    Each write of the Journal costs four fsyncs, nearly all the fsyncs of the suite, and a slow disk, such as that of a
    CI runner, then sets the time of the suite. A test kills processes but never the machine, so it needs no fsync.
    """
    connect = sqlite3.connect

    def without_fsync(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        db: sqlite3.Connection = connect(*args, **kwargs)
        db.execute("PRAGMA synchronous=OFF")
        return db

    sqlite3.connect = without_fsync  # type: ignore[assignment]


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
def factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    """Gives the test a Harness with its fake host CLIs first on the PATH, and fails the test when it leaves a process
    running that a correct Run would end.

    A test whose Run dies on purpose calls `Harness.crashed`, so the processes of the dead Run may stay.
    """
    harness = Harness(tmp_path)
    monkeypatch.setenv("PATH", f"{harness.bin}{os.pathsep}{os.environ['PATH']}")
    yield harness
    left = harness.end()
    assert not left, "the test left processes running:\n" + "\n".join(f"{pid} {cmd}" for pid, cmd in left.items())
