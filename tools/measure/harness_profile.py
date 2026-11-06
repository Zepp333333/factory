"""Pytest plugin that splits the time of each test into git, SQLite, subprocesses, idle ticks and the scripted agent.

It wraps the few calls where a harness test waits on something outside Python:

- `subprocess.Popen` start, `communicate` and `wait`, sorted by the program they run;
- every `execute`, `executemany`, `executescript` and `commit` of a connection from `sqlite3.connect`;
- `FakeClock.sleep`, which is one idle tick of a Run;
- `ScriptedHandle.poll`, which waits for the scripted agent to write its files.

Each thread keeps a stack of open spans, so a span counts only its own time and not the spans inside it.
The rest of a test's wall time is Python, both the factory's and the harness's own.
It writes one JSON line per test to `HARNESS_PROFILE`: the test id, its wall time, whether it built a Harness,
and the seconds and calls of each category.
"""

import functools
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest

GIT = "git"
SQLITE = "sqlite"
AGENT = "scripted agent"
FAKES = "fake CLIs"
SUBPROCESS = "other subprocesses"
IDLE = "idle ticks"

_local = threading.local()
_totals: dict[str, float] = defaultdict(float)
_calls: dict[str, int] = defaultdict(int)
_lock = threading.Lock()
_built_harness = False


class Span:
    """One timed call that is still open, with the time its own timed calls took."""

    def __init__(self) -> None:
        self.start = time.perf_counter()
        self.inner = 0.0


def _stack() -> list[Span]:
    if not hasattr(_local, "stack"):
        _local.stack = []
    stack: list[Span] = _local.stack
    return stack


def timed[**P, R](
    category: Callable[..., str] | str, counts: bool = True
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Wraps a function so its own time, less the timed calls inside it, adds to its category.

    With `counts` each call also counts once for its category, such as one process started or one statement run.
    """

    def wrap(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            name = category(*args, **kwargs) if callable(category) else category
            if counts:
                with _lock:
                    _calls[name] += 1
            stack = _stack()
            span = Span()
            stack.append(span)
            try:
                return func(*args, **kwargs)
            finally:
                stack.pop()
                took = time.perf_counter() - span.start
                with _lock:
                    _totals[name] += took - span.inner
                if stack:
                    stack[-1].inner += took

        return wrapper

    return wrap


def category_of(args: Any) -> str:
    """Sorts a process by what it runs: git, the scripted agent, a fake CLI, or another program by its name.

    A Python process is named by its module, or as an inline script, and `ps` by whether the harness runs it.
    """
    argv = [args] if isinstance(args, str | os.PathLike) else list(args)
    words = [str(arg) for arg in argv]
    name = Path(words[0]).name
    if name == "git":
        return GIT
    if any(Path(word).name == "scripted_agent.py" for word in words):
        return AGENT
    if any(Path(word).name.startswith("fake_") or "import fake_" in word or "from fake_" in word for word in words):
        return FAKES
    if name.startswith("python") and len(words) > 2 and words[1] in ("-m", "-c"):
        name = f"python -m {words[2]}" if words[1] == "-m" else "python -c"
    if name == "ps" and _called_from("harness.py"):
        name = "ps from the harness"
    return f"{SUBPROCESS}: {name}"


def _called_from(file: str) -> bool:
    """Tells whether a frame of `file` is on the call stack, which only CPython's `sys._getframe` shows."""
    frame = sys._getframe(1)
    while frame is not None:
        if Path(frame.f_code.co_filename).name == file:
            return True
        frame = frame.f_back  # type: ignore[assignment]
    return False


def category_of_process(process: subprocess.Popen[Any], *_: Any, **__: Any) -> str:
    return category_of(process.args)


def category_of_start(_: subprocess.Popen[Any], args: Any, *__: Any, **___: Any) -> str:
    return category_of(args)


class TimedConnection(sqlite3.Connection):
    """A connection whose statements count as SQLite time."""

    execute = timed(SQLITE)(sqlite3.Connection.execute)
    executemany = timed(SQLITE)(sqlite3.Connection.executemany)
    executescript = timed(SQLITE)(sqlite3.Connection.executescript)
    commit = timed(SQLITE)(sqlite3.Connection.commit)


def _patch_process_and_sqlite() -> None:
    popen = subprocess.Popen
    popen.__init__ = timed(category_of_start)(popen.__init__)  # type: ignore[method-assign, assignment]
    popen.communicate = timed(category_of_process, counts=False)(popen.communicate)  # type: ignore[method-assign]
    popen.wait = timed(category_of_process, counts=False)(popen.wait)  # type: ignore[method-assign]

    connect: Callable[..., sqlite3.Connection] = sqlite3.connect

    @timed(SQLITE)
    def timed_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs.setdefault("factory", TimedConnection)
        return connect(*args, **kwargs)

    sqlite3.connect = timed_connect  # type: ignore[assignment]


def _patch_harness() -> None:
    harness = sys.modules.get("harness")
    if harness is None:
        return
    harness.FakeClock.sleep = timed(IDLE)(harness.FakeClock.sleep)
    harness.ScriptedHandle.poll = timed(AGENT, counts=False)(harness.ScriptedHandle.poll)
    original_init = harness.Harness.__init__

    @functools.wraps(original_init)
    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        global _built_harness
        _built_harness = True
        original_init(self, *args, **kwargs)

    harness.Harness.__init__ = init


def pytest_configure(config: pytest.Config) -> None:
    _patch_process_and_sqlite()


def pytest_collection_finish(session: pytest.Session) -> None:
    _patch_harness()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None]:
    global _built_harness
    _totals.clear()
    _calls.clear()
    _built_harness = False
    start = time.perf_counter()
    yield
    wall = time.perf_counter() - start
    row = {
        "test": item.nodeid,
        "wall": wall,
        "harness": _built_harness,
        "seconds": dict(_totals),
        "calls": dict(_calls),
    }
    with Path(os.environ["HARNESS_PROFILE"]).open("a") as out:
        out.write(json.dumps(row) + "\n")
