"""Every git write the engine makes goes through HostGit."""

import os
import subprocess
from pathlib import Path

IDENTITY = {
    "GIT_AUTHOR_NAME": "Software Factory",
    "GIT_AUTHOR_EMAIL": "factory@localhost",
    "GIT_COMMITTER_NAME": "Software Factory",
    "GIT_COMMITTER_EMAIL": "factory@localhost",
}

WRITABLE_PREFIX = "factory/"


class GitError(Exception):
    pass


class HostGit:
    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        env = {**os.environ, **IDENTITY}
        proc = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=cwd or self.repo,
            env=env,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def resolve(self, ref: str) -> str:
        return self._git("rev-parse", "--verify", f"{ref}^{{commit}}")

    def create_branch(self, branch: str, commit: str) -> None:
        """Creates a branch and refuses to overwrite an existing one."""
        self._git("update-ref", self._ref(branch), commit, "")

    def fast_forward(self, branch: str, new: str, expected: str) -> None:
        """Moves a branch to `new` only if it still points at `expected` and `new` descends from it."""
        if self._git("merge-base", expected, new) != expected:
            raise GitError(f"{branch}: {new} does not descend from {expected}")
        self._git("update-ref", self._ref(branch), new, expected)

    def head(self, workspace: Path) -> str:
        return self._git("rev-parse", "HEAD", cwd=workspace)

    def commit_all(self, workspace: Path, subject: str, trailers: dict[str, str]) -> str | None:
        """Commits every change in the Workspace, and returns the new head or None when nothing changed."""
        self._git("add", "-A", cwd=workspace)
        if not self._git("status", "--porcelain", cwd=workspace):
            return None
        body = "\n".join(f"{key}: {value}" for key, value in trailers.items())
        self._git("commit", "-q", "--no-verify", "-m", subject, "-m", body, cwd=workspace)
        return self.head(workspace)

    def discard(self, workspace: Path, head: str) -> tuple[str, list[str]]:
        """Resets the Workspace to `head` and returns the binary patch and the files of what it threw away.

        Ignored files stay, because they are the Workspace's build cache.
        """
        self._git("add", "-A", cwd=workspace)
        patch = self._git("diff", "--cached", "--binary", head, cwd=workspace)
        files = self._git("diff", "--cached", "--name-only", head, cwd=workspace).splitlines()
        self._git("reset", "-q", "--hard", head, cwd=workspace)
        self._git("clean", "-fdq", cwd=workspace)
        return (patch + "\n" if patch else ""), files

    def _ref(self, branch: str) -> str:
        if not branch.startswith(WRITABLE_PREFIX):
            raise GitError(f"the engine writes only to {WRITABLE_PREFIX}* branches, not {branch}")
        return f"refs/heads/{branch}"
