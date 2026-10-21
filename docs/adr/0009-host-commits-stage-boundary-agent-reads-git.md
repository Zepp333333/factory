# The host commits the Stage boundary, and the agent reads git read-only

The Workspace stays a git worktree on the host, and the Sandbox mounts it read-write and the whole shared `.git` read-only.
The agent sees the history with plain git (`status`, `diff`, `log`, `show`, `blame`), but cannot create `index.lock`, so it does not commit and does not run `stash` or `restore`.
After every finished Attempt of a Stage with `workspace: rw` the host runs `git add -A` and makes one commit with the trailers `Factory-Run`, `Factory-Ticket`, `Factory-Stage`, `Factory-Attempt` from the factory's fixed identity.
The first line of the commit is the `summary` from the role's output if there is one, otherwise `<stage>: <ticket>`; without changes there is no commit.
The Attempt's artifact stores `head_before` and `head_after`, and the Brief carries the Ticket's `base` and the commit range of every input Stage.
The agent calls `git diff` over these ranges itself; the Orchestrator does not materialize a diff file.

## Considered Options

- A clone of its own in the Sandbox (`sbx create --clone`).
  Rejected because the files are not visible on the host until `fetch` and the Workbench does not show a live diff, a human cannot edit the Workspace while waiting on a Decision, `--clone` does not run from a linked worktree, and the approach itself is tied to `sbx`.
- The agent commits by itself into the shared `.git`, with `:ro` on `hooks/` and `config` (path A from the `sbx` check).
  Rejected because the agent could move `main` or another Ticket's branch, rewrite `commondir`, and the host's hooks run in the VM.
- An agent without git, with the diff arriving as an artifact file.
  Rejected because the agent loses `log` and `blame`, and the patch duplicates what git already stores.
- A Sandbox per Attempt and a fresh Sandbox per Probe.
  Rejected because every start costs 8-10 s and loses the VM's environment, while isolation between Stages of one Ticket protects nothing.
- Leave the edits of a failed Attempt to the next one as a hint.
  Rejected because the same Brief would then give a different start.
- A strict `workspace: ro` through a separate Sandbox with a ro mount.
  Rejected in favor of one Sandbox per Ticket; a violation is caught after the Attempt.

## Consequences

- The price of the choice: the factory's skills do not count on commits and `stash` inside an Attempt, and there are no checkpoints inside an Attempt.
- All of the host's git calls in a Workspace run with explicit `GIT_DIR`, `GIT_COMMON_DIR`, `GIT_WORK_TREE` and with `-c core.hooksPath=/dev/null -c core.fsmonitor=false`.
  Before them the host overwrites the worktree's `.git` file with known contents, so that a swap by the agent does not break git for the Workbench and the human.
  In the same way the host returns the worktree's HEAD to the Ticket's branch, so `reset` and the commit move only that branch.
  The host does not enter repositories the agent nested in the Workspace (`diff.ignoreSubmodules=all`, `submodule.recurse=false`), and a Workspace reset removes them with `clean -ffd`.
  The Stage boundary commit is built with `write-tree` and `commit-tree`, and `update-ref` moves the ref with a check of the old value.
- The Sandbox lives for the whole Ticket: Attempts and Probes run in it as separate processes, it is stopped while waiting for a human (ADR 0007), and it is removed after `done` or `abandoned`.
- A Probe starts after the Attempt's commit, that is on the committed state.
  Ignored files (`build/`, venv) stay as a cache between Stages; a binary planted in the cache is handled by the Gate option `clean: true`, which cleans out ignored files before the Probe.
- A failed, lost or timed-out Attempt leaves its uncommitted edits in the Artifact `discarded.patch`, after which the host runs `reset --hard <head_before>` and `clean -fd` without ignored files.
  The patch is there for a human to debug and does not get into the Brief.
  If the Attempt started in a merge state after a `sync` with the outcome `conflict` (ADR 0008), the host repeats the same merge of the same tip after the reset, to bring back the markers and `MERGE_HEAD`.
- `workspace: rw | ro` stops being a temporary field of ADR 0004 and means "whether the host commits the Stage's result".
  After an Attempt of an `ro` Stage, changes in tracked and non-ignored files go into `discarded.patch`, the Workspace is reset, a violation event is written to the Journal, and the Attempt counts.
- A human's edits while waiting on a Decision go through in the same form as an agent's edits.
  Having accepted the Command, the Orchestrator checks that HEAD is a descendant of the last Attempt's `head_after`, commits uncommitted edits with the trailer `Factory-Actor: human` and writes the wait's `head_before` and `head_after` into the Decision's outcome artifact, which the next Stage gets through `cause`.
  If the human rewrote history, the Command is rejected with a message, and the Decision keeps waiting.
- The commit of an Attempt of the Stage `resolve` with `MERGE_HEAD` is the merge commit from ADR 0008.
- `base` in the Brief is the last tip of the integration branch brought into the Ticket (the Workspace's base or the last `sync`), so `git diff base..HEAD` shows only the Ticket's work.
- A ro mount of the shared `.git` in N Sandboxes must see the host's commits at once; the VM's file system cache delay has to be checked during the build.
