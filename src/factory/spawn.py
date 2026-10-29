"""Every short host tool call, such as `git`, `gh` or `sbx`, starts through `run`."""

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence


def run(
    argv: Sequence[str], env: Mapping[str, str] | None = None, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Runs a tool with no stdin and returns its exit code and output as text, and raises OSError if it cannot start.

    `subprocess` starts a child with `posix_spawn` only when the tool is named by its path, no `cwd` is given
    and `close_fds` is off, and a fork of the engine's large process costs more than most git calls take.
    So `run` looks the tool up on the PATH itself and takes no `cwd`, and a tool that needs one gets it as a flag,
    such as `git -C`.
    The child still gets only its stdio, because Python opens every other file descriptor non-inheritable.
    """
    path = (env if env is not None else os.environ).get("PATH")
    executable = shutil.which(argv[0], path=path) or argv[0]
    return subprocess.run(
        [executable, *argv[1:]],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        close_fds=False,
    )
