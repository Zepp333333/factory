# Acceptance run 2026-10-30 - 2026-11-02: a huge private C++ repo (#48)

Runs of a project Pipeline `characterize` on a Spec in the repository's self-managed GitLab, through `GitLabTracker`.
The Spec is a map of characterization-test Tickets for the C++ engine, one Ticket per source area.
All agents ran in `SbxSandbox` on the subscription, on Opus unless noted.
Sources: the Journal in `factory.db`, the Artifacts in `runs/<id>/` and the agent transcripts.

## Setup

- Pipeline `characterize`: Stage `characterize`, Gates for the build, the repo rules and test order, Stage `review`, Operation `file_issues`, a model Decision `acceptance`, then `sync` and `merge`.
- The Sandbox template bakes in the toolchain, the apt dependencies, the codegen venv, the pinned third-party dependency and a warm ccache.
  So the project file sets `egress: []` instead of the egress list the Ticket first planned.
- `max_parallel: 2`, 6 CPUs and 12 GB per Sandbox on a 14-core host.
- `caches: [build, third_party]` keep the CMake build between Attempts.
- `ci: {timeout: 120m}`, because the MR pipeline builds the third-party dependency on its runners.
- The MRs target a non-default branch, so `Closes #N` does not close GitLab issues, and the owner closes the Tickets by hand.

## Runs

| Run | Tickets | Outcome | Duration | Cost |
|---|---|---|---|---|
| `20261102-110854-2d60` | 22 | `done` | 25 min | 3.87 USD |
| `20261102-115326-a6af` | 30, 34, 23, 32 (Sonnet) | `partial`, 23 and 32 `abandoned` | 48 min | 10.30 USD |
| `20261102-131047-ede5` | 23, 32 | `done` | 26 min | 7.73 USD |
| `20261102-135306-182b` | 24, 25, 26, 31, 33, 35 | `done` | 57 min | 20.89 USD |
| `20261102-154131-a680` | 27, 28, 29, 36, 39, 44 | `done` | 74 min | 19.51 USD |

Ticket 21 went through earlier Runs while the engine fixes below landed.
Each Run landed as one MR from `factory/<run-id>` into the base branch.

## Outcome

- 18 Tickets reached `merged`, and every one of them is in a merged MR.
- With Opus, a Ticket costs 2.3 - 5.0 USD and takes 10 - 20 min, and two Tickets run at once.
- Several Sandboxes at once read the shared `.git` through the ro mount and saw the host's commits, and every `sync` worked.
- The engine never lost work in a crash, and an orphaned Run resumed from its inbox with `factory resume`.

## Findings and fixes

- The engine hard-coded the PR body headings, so the project's Describer failed the check (#188, fixed by #189).
- A GitLab pipeline that stops at a manual job counted as pending forever (fixed by #190).
- Test runs leaked renderer and scripted agent processes (#191, fixed by #193).
- The Run tree stacked frames on a narrow terminal (fixed by #192).
- `file_issues` abandoned a Ticket whose fix Round listed a key an earlier Stage had already filed (#197, fixed by #198).
- An agent's placeholder mistake failed `file_issues` and abandoned the Ticket, although a fix Round can repair it (#199, fixed by #200 with outcome `refused`).
- A coverage build writes `default.profraw` into the Workspace, and the engine failed the Attempt for a stray ignored file.
  The project now sets `LLVM_PROFILE_FILE` outside the Workspace.
  The engine still throws away the Attempt's work for such a file (#202, open).
- A Gate reruns its Probe on a tree it already passed in the same Ticket (#203, open).
- A runner system failure in GitLab CI stops the Run until the owner retries the job and answers `recheck`.

## Speed and cost

- In Ticket 22, `characterize` took 9 min 10 s, and its tools took only 72 s of that.
  The model's own time dominates, so fewer test runs would save at most about 1.5 min per Ticket.
- Sonnet on the implementing Stages (Run `a6af`) was neither faster nor cheaper.
  All four Tickets needed rework for the finding rules or a key mismatch, so the project went back to Opus.
- A per-node reasoning effort would let a project tune this without changing the model (#196, open).

## Open

- GitLab title matching reused no issue when an agent reworded a seam that an earlier Ticket had filed, so the owner closed one duplicate by hand.
- The map still has its remaining Tickets, which the owner runs in further Runs with the same setup.
