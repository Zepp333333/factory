"""A SleepBlock keeps the computer from sleeping when idle while a Run's process is alive.

The helper is tied to the Run's pid, so the system releases the block when the process ends, even if it crashed.
The display still turns off, because the helper blocks idle sleep only.
"""

import shutil
import subprocess
from dataclasses import dataclass
from typing import Self

# `caffeinate -i` blocks idle sleep and `-w` makes it exit with the pid.
# `tail --pid` waits for the pid, so the inhibitor lock lives exactly as long as the Run's process.
HELPERS = {
    "darwin": lambda pid: ["caffeinate", "-i", "-w", str(pid)],
    "linux": lambda pid: [
        "systemd-inhibit",
        "--what=idle:sleep",
        "--who=factory",
        "--why=A Run is going",
        "tail",
        f"--pid={pid}",
        "-f",
        "/dev/null",
    ],
}

# How long `release` waits for the helper to exit after it asked, in seconds.
RELEASE_SECONDS = 5


@dataclass
class SleepBlock:
    """SleepBlock is the helper process that blocks sleep, or the reason there is none."""

    argv: list[str]
    reason: str | None = None
    process: "subprocess.Popen[bytes] | None" = None

    @classmethod
    def acquire(cls, platform: str, pid: int) -> Self:
        """Starts the helper of `platform` for `pid`, or returns a block that says why it cannot."""
        helper = HELPERS.get(platform)
        if helper is None:
            return cls([], f"no way to block sleep is known on {platform}")
        argv = helper(pid)
        if shutil.which(argv[0]) is None:
            return cls(argv, f"{argv[0]} is not installed")
        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError as error:
            return cls(argv, f"{argv[0]} did not start: {error}")
        return cls(argv, process=process)

    def release(self) -> None:
        """Stops the helper at once, as the end of the Run's process would."""
        process, self.process = self.process, None
        if process is None:
            return
        process.terminate()
        try:
            process.wait(RELEASE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
