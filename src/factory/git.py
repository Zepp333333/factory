"""Every git write the engine makes goes through HostGit."""

import os
import re
import shutil
from pathlib import Path

from factory import spawn

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


# A line the merge writes around a conflict, which a resolved file no longer holds.
MARKER = re.compile(r"^(<<<<<<<|>>>>>>>)( .*)?$", re.MULTILINE)

# The header of a hunk in `git diff -U0`, with the first line and the count of lines on the new side.
HUNK = re.compile(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@")


class GitError(Exception):
    """GitError holds the exit code of the git call that failed, or None when no git call did."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class MergeConflict(GitError):
    """MergeConflict holds the files that the merge left with conflict markers."""

    def __init__(self, message: str, code: int | None = None, files: list[str] | None = None) -> None:
        super().__init__(message, code)
        self.files = files or []


class HostGit:
    """HostGit runs git on the host against the Target project's own `.git`, whatever a Workspace points at.

    Every call names `GIT_DIR` explicitly.
    A call in a Workspace first restores the worktree's `.git` file and pins its HEAD to the Ticket branch,
    because the agent can rewrite both.
    """

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        self.common = common_dir(repo)
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
        It raises MergeConflict when the two conflict.
        The branch stays where it was, and the Workspace holds the merge with its markers and `MERGE_HEAD`,
        for `resolve` to edit and `commit_all` to commit.
        Any other failure of `merge-tree` raises a plain GitError.
        """
        head = self._git_in(workspace, branch, "rev-parse", "HEAD")
        if self._git("merge-base", tip, head) == tip:
            return head
        try:
            tree = self._git("merge-tree", "--write-tree", head, tip)
        except GitError as error:
            # `merge-tree` exits with 1 on a conflict and with another code when it cannot merge at all.
            if error.code != 1:
                raise
            files = self.begin_merge(workspace, branch, tip)
            raise MergeConflict(f"{branch} conflicts with {tip}: {error}", files=files) from error
        message = f"Merge {tip} into {branch}"
        commit = self._git_in(workspace, branch, "commit-tree", tree, "-p", head, "-p", tip, "-m", message)
        self._update_ref(f"refs/heads/{branch}", commit, head)
        self._git_in(workspace, branch, "reset", "-q", "--hard", commit)
        return commit

    def begin_merge(self, workspace: Path, branch: str, tip: str) -> list[str]:
        """Starts the merge of `tip` in the Workspace without committing, and returns the files left in conflict."""
        try:
            self._git_in(workspace, branch, "merge", "--no-commit", "--no-ff", "-q", tip)
        except GitError as error:
            if error.code != 1:
                raise
        return self._git_in(workspace, branch, "diff", "--name-only", "--diff-filter=U").splitlines()

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Returns whether `descendant` already holds `ancestor`."""
        try:
            self._git("merge-base", "--is-ancestor", ancestor, descendant)
        except GitError as error:
            if error.code != 1:
                raise
            return False
        return True

    def markers(self, workspace: Path, files: list[str]) -> list[str]:
        """Returns the files among `files` that still hold a conflict marker line.

        A conflict without markers, such as a file deleted on one side or a binary file, never shows up here,
        so the merge commit takes whatever the Workspace holds for it.
        """
        left = []
        for name in files:
            path = workspace / name
            if path.is_file() and not path.is_symlink() and MARKER.search(path.read_text(errors="replace")):
                left.append(name)
        return left

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

    def branches(self, prefix: str) -> list[str]:
        """Returns the names of the local branches that start with `prefix`."""
        out = self._git("for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}*")
        return out.splitlines()

    def worktrees(self) -> list[Path]:
        """Returns the path of every worktree the Target project lists, its own checkout included."""
        out = self._git("worktree", "list", "--porcelain")
        return [Path(line.removeprefix("worktree ")) for line in out.splitlines() if line.startswith("worktree ")]

    def uncommitted(self, workspace: Path) -> list[str]:
        """Returns the files of the Workspace that are modified or untracked, without pinning or locking it."""
        gitdir = self._worktree_gitdir(workspace)
        env = host_env(GIT_DIR=str(gitdir), GIT_COMMON_DIR=str(self.common), GIT_WORK_TREE=str(workspace))
        out = run(["--no-optional-locks", "status", "--porcelain"], env, cwd=workspace)
        return [line[3:] for line in out.splitlines()]

    def remotes(self) -> dict[str, str]:
        """Returns each remote's URL as the config states it, before any `insteadOf` rewrites it."""
        names = self._git("remote").splitlines()
        return {name: self._git("config", "--get", f"remote.{name}.url") for name in names}

    def push(self, remote: str, branch: str) -> None:
        """Pushes the branch to the same name on `remote` without asking for credentials on the terminal."""
        ref = writable(f"refs/heads/{branch}")
        env = host_env(GIT_DIR=str(self.common), GIT_TERMINAL_PROMPT="0")
        run(["push", "--quiet", remote, f"{ref}:{ref}"], env, cwd=self.repo)

    def fetch(self, remote: str, branch: str) -> str:
        """Fetches `branch` from `remote` and returns its commit, without moving any local branch or the working tree.

        It never asks for credentials on the terminal.
        """
        env = host_env(GIT_DIR=str(self.common), GIT_TERMINAL_PROMPT="0")
        run(["fetch", "--quiet", remote, f"refs/heads/{branch}"], env, cwd=self.repo)
        return self._git("rev-parse", "--verify", "FETCH_HEAD^{commit}")

    def missing_from(self, ref: str, commit: str) -> list[str]:
        """Returns the one-line summaries of the commits `ref` holds and `commit` lacks, newest first."""
        return self._git("log", "--oneline", "--no-decorate", f"{commit}..{ref}").splitlines()

    def head(self, workspace: Path, branch: str) -> str:
        return self._git_in(workspace, branch, "rev-parse", "HEAD")

    def commit_all(
        self, workspace: Path, branch: str, subject: str, trailers: dict[str, str], merging: str | None = None
    ) -> str | None:
        """Commits every change in the Workspace onto `branch` and returns the new head, or None if nothing changed.

        With `merging` set, the Workspace holds a merge of that commit, and the commit is the merge commit,
        which is made even if the files came out equal to the branch.
        """
        self._git_in(workspace, branch, "add", "-A")
        if merging is None and not self._git_in(workspace, branch, "status", "--porcelain"):
            return None
        parent = self._git_in(workspace, branch, "rev-parse", "HEAD")
        tree = self._git_in(workspace, branch, "write-tree")
        body = "\n".join(f"{key}: {value}" for key, value in trailers.items())
        parents = ["-p", parent] + (["-p", merging] if merging else [])
        commit = self._git_in(workspace, branch, "commit-tree", tree, *parents, "-m", subject, "-m", body)
        self._update_ref(f"refs/heads/{branch}", commit, parent)
        if merging:
            # This ends the merge, which removes `MERGE_HEAD`.
            self._git_in(workspace, branch, "reset", "-q", "--hard", commit)
        return commit

    def changed_lines(self, workspace: Path, branch: str, base: str) -> dict[str, frozenset[int]]:
        """Returns the lines the Ticket branch's head added or changed since `base`, by file."""
        diff = self._git_in(
            workspace, branch, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", base, "HEAD"
        )
        changed: dict[str, set[int]] = {}
        current: set[int] | None = None
        for line in diff.splitlines():
            if line.startswith("+++ "):
                current = None if line == "+++ /dev/null" else changed.setdefault(line[6:], set())
            elif line.startswith("@@") and current is not None:
                match = HUNK.match(line)
                if match:
                    start, count = int(match.group(1)), int(match.group(2) or 1)
                    current.update(range(start, start + count))
        return {path: frozenset(lines) for path, lines in changed.items() if lines}

    def size(self, base: str, head: str) -> dict[str, int]:
        """Returns the files, added lines and removed lines of the change from `base` to `head`.

        A binary file counts as a file and adds no lines.
        """
        rows = [line.split("\t") for line in self._git("diff", "--numstat", "--no-renames", base, head).splitlines()]
        return {
            "files_changed": len(rows),
            "lines_added": sum(int(added) for added, _, _ in rows if added.isdigit()),
            "lines_removed": sum(int(removed) for _, removed, _ in rows if removed.isdigit()),
        }

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

    def ignored(self, workspace: Path, branch: str) -> frozenset[str]:
        """Returns the untracked files in the Workspace that `.gitignore` hides from the Stage boundary commit."""
        out = self._git_in(
            workspace, branch, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--no-empty-directory"
        )
        return frozenset(name for name in out.split("\0") if name)

    def files(self, workspace: Path, branch: str) -> list[str]:
        """Returns the tracked files of the Workspace and its untracked ones that `.gitignore` does not hide."""
        out = self._git_in(workspace, branch, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        return sorted({name for name in out.split("\0") if name})

    def remove_untracked(self, workspace: Path, branch: str, names: list[str]) -> None:
        """Removes untracked files and nested repositories by name, ignored ones too."""
        if names:
            self._git_in(workspace, branch, "clean", "-ffdxq", "--", *(f":(literal){name}" for name in names))

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
    proc = spawn.run(["git", "-C", str(cwd), *SAFE_CONFIG, *args], env)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip()}", proc.returncode)
    return proc.stdout.strip()


def common_dir(repo: Path) -> Path:
    """Returns the git dir that every worktree of `repo` shares, or raises GitError."""
    return Path(run(["rev-parse", "--path-format=absolute", "--git-common-dir"], host_env(), cwd=repo)).resolve()


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
