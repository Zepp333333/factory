"""Every git write the engine makes goes through HostGit."""

import os
import shutil
import subprocess
from pathlib import Path

IDENTITY = {
    "GIT_AUTHOR_NAME": "Software Factory",
    "GIT_AUTHOR_EMAIL": "factory@localhost",
    "GIT_COMMITTER_NAME": "Software Factory",
    "GIT_COMMITTER_EMAIL": "factory@localhost",
}

# The engine writes refs only under these prefixes.
WRITABLE_REFS = ("refs/heads/factory/", "refs/sandboxes/", "refs/factory/")

# Agents write the Workspace, so no host git call may run a hook or an fsmonitor configured anywhere,
# or step into a repository the agent nested in the Workspace and run what its config names.
SAFE_CONFIG = [
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "diff.ignoreSubmodules=all",
    "-c",
    "submodule.recurse=false",
]


class GitError(Exception):
    pass


class MergeConflict(GitError):
    pass


class HostGit:
    """HostGit runs git on the host against the Target project's own `.git`, whatever a Workspace points at.

    Every call names `GIT_DIR` explicitly.
    A call in a Workspace first restores the worktree's `.git` file and pins its HEAD to the Ticket branch,
    because the agent can rewrite both.
    """

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        common = run(["rev-parse", "--path-format=absolute", "--git-common-dir"], host_env(), cwd=repo)
        self.common = Path(common).resolve()
        self._gitdirs: dict[Path, Path] = {}

    def resolve(self, ref: str) -> str:
        return self._git("rev-parse", "--verify", f"{ref}^{{commit}}")

    def create_branch(self, branch: str, commit: str) -> None:
        """Creates a branch and refuses to overwrite an existing one."""
        self._update_ref(f"refs/heads/{branch}", commit, "")

    def fast_forward(self, branch: str, new: str, expected: str) -> None:
        """Moves a branch to `new` only if it still points at `expected` and `new` descends from it."""
        if self._git("merge-base", expected, new) != expected:
            raise GitError(f"{branch}: {new} does not descend from {expected}")
        self._update_ref(f"refs/heads/{branch}", new, expected)

    def catch_up(self, workspace: Path, branch: str, tip: str) -> str:
        """Merges `tip` into the Ticket branch and the Workspace, and returns the branch's head.

        The head stays as it is when it already holds `tip`.
        It raises MergeConflict when the two conflict, and then leaves the branch where it was.
        """
        head = self._git_in(workspace, branch, "rev-parse", "HEAD")
        if self._git("merge-base", tip, head) == tip:
            return head
        try:
            tree = self._git("merge-tree", "--write-tree", head, tip)
        except GitError as error:
            raise MergeConflict(f"{branch} conflicts with {tip}: {error}") from error
        message = f"Merge {tip} into {branch}"
        commit = self._git_in(workspace, branch, "commit-tree", tree, "-p", head, "-p", tip, "-m", message)
        self._update_ref(f"refs/heads/{branch}", commit, head)
        self._git_in(workspace, branch, "reset", "-q", "--hard", commit)
        return commit

    def delete_branch(self, branch: str, expected: str) -> None:
        """Deletes a branch only if it still points at `expected`."""
        self._update_ref(f"refs/heads/{branch}", None, expected)

    def keep(self, ref: str, commit: str) -> None:
        """Points `ref` at `commit`, or leaves it alone when it already does, and refuses to move it elsewhere."""
        try:
            self._update_ref(ref, commit, "")
        except GitError:
            if self._git("rev-parse", "--verify", ref) != commit:
                raise

    def remotes(self) -> dict[str, str]:
        """Returns each remote's URL as the config states it, before any `insteadOf` rewrites it."""
        names = self._git("remote").splitlines()
        return {name: self._git("config", "--get", f"remote.{name}.url") for name in names}

    def push(self, remote: str, branch: str) -> None:
        """Pushes the branch to the same name on `remote` without asking for credentials on the terminal."""
        ref = writable(f"refs/heads/{branch}")
        env = host_env(GIT_DIR=str(self.common), GIT_TERMINAL_PROMPT="0")
        run(["push", "--quiet", remote, f"{ref}:{ref}"], env, cwd=self.repo)

    def head(self, workspace: Path, branch: str) -> str:
        return self._git_in(workspace, branch, "rev-parse", "HEAD")

    def commit_all(self, workspace: Path, branch: str, subject: str, trailers: dict[str, str]) -> str | None:
        """Commits every change in the Workspace onto `branch` and returns the new head, or None if nothing changed."""
        self._git_in(workspace, branch, "add", "-A")
        if not self._git_in(workspace, branch, "status", "--porcelain"):
            return None
        parent = self._git_in(workspace, branch, "rev-parse", "HEAD")
        tree = self._git_in(workspace, branch, "write-tree")
        body = "\n".join(f"{key}: {value}" for key, value in trailers.items())
        commit = self._git_in(workspace, branch, "commit-tree", tree, "-p", parent, "-m", subject, "-m", body)
        self._update_ref(f"refs/heads/{branch}", commit, parent)
        return commit

    def discard(self, workspace: Path, branch: str, head: str) -> tuple[str, list[str]]:
        """Resets the Workspace to `head` and returns the binary patch and the files of what it threw away.

        Ignored files stay, because they are the Workspace's build cache.
        """
        self._git_in(workspace, branch, "add", "-A")
        patch = self._git_in(workspace, branch, "diff", "--cached", "--binary", head)
        files = self._git_in(workspace, branch, "diff", "--cached", "--name-only", head).splitlines()
        self._git_in(workspace, branch, "reset", "-q", "--hard", head)
        self._git_in(workspace, branch, "clean", "-ffdq")
        return (patch + "\n" if patch else ""), files

    def reset(self, workspace: Path, branch: str, ignored: bool) -> None:
        """Puts the Workspace back on the Ticket branch's head and removes untracked files.

        Ignored files stay unless `ignored` asks to remove them too.
        """
        self._git_in(workspace, branch, "reset", "-q", "--hard", "HEAD")
        self._git_in(workspace, branch, "clean", "-ffdxq" if ignored else "-ffdq")

    def _update_ref(self, ref: str, new: str | None, expected: str) -> None:
        """Moves a ref, or deletes it when `new` is None, with a compare-and-swap on `expected`.

        It is the only place the engine writes a ref by name.
        """
        if new is None:
            self._git("update-ref", "-d", writable(ref), expected)
        else:
            self._git("update-ref", writable(ref), new, expected)

    def _git(self, *args: str) -> str:
        return run(list(args), host_env(GIT_DIR=str(self.common)), cwd=self.repo)

    def _git_in(self, workspace: Path, branch: str, *args: str) -> str:
        gitdir = self.pin(workspace, branch)
        env = host_env(GIT_DIR=str(gitdir), GIT_COMMON_DIR=str(self.common), GIT_WORK_TREE=str(workspace))
        return run(list(args), env, cwd=workspace)

    def pin(self, workspace: Path, branch: str) -> Path:
        """Restores the Workspace's `.git` file and HEAD from the Target project's `.git` and returns its git dir.

        The engine moves the Ticket branch, so a pinned HEAD keeps `reset` from moving any other ref.
        A git call the engine does not wrap, such as `git worktree remove`, runs only after a `pin`.
        """
        gitdir = self._worktree_gitdir(workspace)
        restore(workspace / ".git", f"gitdir: {gitdir}\n")
        restore(gitdir / "HEAD", f"ref: {writable(f'refs/heads/{branch}')}\n")
        return gitdir

    def _worktree_gitdir(self, workspace: Path) -> Path:
        """Finds the worktree's git dir by the back pointer git keeps in the Target project's `.git`."""
        root = workspace.resolve()
        if root not in self._gitdirs:
            for gitdir in sorted((self.common / "worktrees").glob("*")):
                back = gitdir / "gitdir"
                if back.is_file() and Path(back.read_text().strip()).parent.resolve() == root:
                    self._gitdirs[root] = gitdir
                    break
            else:
                raise GitError(f"{workspace} is not a worktree of {self.repo}")
        return self._gitdirs[root]


def run(args: list[str], env: dict[str, str], cwd: Path) -> str:
    """Runs a host git call with SAFE_CONFIG and returns its output, or raises GitError."""
    proc = subprocess.run(["git", *SAFE_CONFIG, *args], cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def host_env(**git: str) -> dict[str, str]:
    """Returns the environment for a host git call, with only the `GIT_*` variables the caller names."""
    inherited = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return {**inherited, **IDENTITY, **git}


def writable(ref: str) -> str:
    """Returns `ref` if the engine may write it, and raises otherwise."""
    if not ref.startswith(WRITABLE_REFS):
        raise GitError(f"the engine writes only under {', '.join(WRITABLE_REFS)}, not {ref}")
    return ref


def restore(path: Path, content: str) -> None:
    """Makes `path` a regular file holding `content`, whatever the agent left there."""
    if not path.is_symlink() and path.is_file() and path.read_text(errors="replace") == content:
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    path.write_text(content)
