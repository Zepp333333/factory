# Merge is an Operation in the Pipeline under the Merge window, and the integration branch moves only fast-forward

A Ticket gets into the integration branch through Pipeline nodes of a new kind, Operation, not through a separate queue of the Run.
An Operation is a deterministic node in which the Orchestrator itself changes a Workspace or the Run's branches on the host; there is no agent in it.
Operations live in the `operations` section with a `kind` from a closed set: `sync` (outcomes `clean | conflict`) and `merge` (outcome `merged`).
`sync` brings the current tip of the integration branch into the Ticket's branch, after it the Gates run on the combined tree in the Ticket's Sandbox, and `merge` moves the integration branch only fast-forward.
The Merge window gives the right to the integration branch to one Ticket at a time, in FIFO order.
The window is held only across Operations and Gates; a Ticket entering a Stage or Decision releases the window, and the Ticket queues up again.
Inside the window the tip does not move, so the integration branch holds only trees that passed the Gates.

## Considered Options

- A Run-level merge queue outside the Pipeline, which a Ticket enters on `done`.
  Rejected because conflicts and escalation would need a second mechanism next to the Pipeline, and ADR 0007 has already rejected a second path to a human.
- Merge as a Gate.
  Rejected because a Gate only checks and changes nothing.
- `git merge --no-ff` into the integration branch with Gates after it.
  Rejected because a broken result already sits in the branch, and the next Tickets start from it.
- Squash into one commit per Ticket.
  Rejected because it loses the commits at Stage boundaries, which handing code between Stages may rely on.
- Optimistic concurrency: `sync` and Gates for many Tickets at once, and `merge` with the outcome `stale` when the tip has moved.
  Rejected because Tickets can yield to each other endlessly, and protection against starvation would be needed.
  The price of the window is that Gates on the combined tree run one Ticket at a time (about 5 minutes per Ticket for a huge private C++ repo); batching and bisection as in Gas Town's Refinery can be added later.
- The agent itself runs `git merge` in the Sandbox on a conflict.
  Rejected because this needs git in the Sandbox, while all git writes must stay on the host.
- A semantic conflict as a Run-level Decision.
  Rejected because it shows up as a Gate `fail` after `sync` and already goes through the usual fix loop.
- A final review Stage over the whole integration branch, as in `implement-spec`.
  Deferred, because it needs a Run-level Pipeline with its own Workspace and Sandbox; in v1 a human looks at the whole branch in the PR.

## Consequences

- The Orchestrator creates the integration branch `factory/<run-id>` at the Run's start from the base ref in the project file, as freshly fetched from the remote the Run pushes to, and writes its initial commit to the Journal.
- The engine writes only to `factory/*` and `refs/sandboxes/*`; it does not touch `main` and other branches.
- A new Workspace starts from the current tip of the integration branch, and this commit is written to the Journal as the Ticket's base.
- A Ticket stops blocking other Tickets on `merged`, not on `done` of its Stages.
- All git calls of Operations run with explicit `GIT_DIR`, `GIT_COMMON_DIR` and `GIT_WORK_TREE`.
- `merge` does `update-ref` with a check of the tip's old value.
- With `max_parallel` above 1, `factory check` rejects a Pipeline in which a path from a Stage, a Decision or the start reaches `merge` through Gates and Operations without `sync`.
  On such a path the tip can move, and `merge` would no longer be a fast-forward.
- On a textual conflict `sync` leaves markers in the working tree with `MERGE_HEAD` and goes to `conflict`.
  The Stage `resolve` only edits files; when it ends, the host checks that no markers are left and makes the merge commit.
- The `sync` artifact lists the conflicting files and the Tickets merged into the integration branch after the Ticket's base, with pointers to their rendered Ticket files; `resolve` gets it as `cause`.
- The template Pipeline runs only Gates after a clean `sync`; after `resolve` the path goes through the reviewer and the risk Decision from ADR 0007, then back into `sync`.
- After `merged` the Orchestrator stops and removes the Sandbox, removes the worktree through the Workbench and deletes the Ticket's branch.
  For `abandoned` it does the same, but the branch head is first saved to `refs/factory/<run-id>/abandoned/<ticket>`.
- Tickets that depend on an `abandoned` Ticket do not start; when there is nowhere left to move, the Run ends as `partial`.
- When all Tickets are `merged` or `abandoned`, the Tracker projection opens a PR from the integration branch into the base ref and lists the abandoned Tickets and everything they blocked.
  Merging the PR is a human's job outside the Run.
