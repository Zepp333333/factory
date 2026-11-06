# A Run ends when CI on its final PR is green or the owner decides, and Gates cannot stand in for CI

A Run used to count its work as done once it opened its final PR.
Run `20261028-180813-b500` ended `done` with PR #137, and CI failed on that PR in two tests that depended on the machine's speed: `FakeClock` moves on every idle tick, and a slow runner made the Run last `1s` where the tests pinned `0s`.
The Gates had passed in the Sandbox on the same commit.

Gates run the same commands as CI, but on another machine, with another OS, speed and environment, so they cannot rule such a failure out.
Only the Target project's own CI on the PR's head commit tells whether the work is done.

These steps are nodes of the default Run graph of ADR 0014: `wait_ci`, the Run Stage `ci_fix` and the Decision `ci_failed`, which a project can route otherwise.

So after `PullRequestOpened` the Run enters phase `ci` and waits for the checks of the pushed head commit through the `PullRequests` port the Run picked from the git remote of the Target project: `gh pr checks` on GitHub, the MR's head pipeline on GitLab.
Checks of any other commit do not count.
The node `wait_ci` ends `green`, `red`, `absent` or `timeout`.
Green CI ends the Run `done`.
CI that does not end within `ci.timeout`, or red CI the fixer has no Round left for, goes to the Decision node `ci_failed` with the outcomes `recheck` and `accept`, answered through `factory decide <run> @run <outcome>` like `infra_failure` (ADR 0007).
A Run graph that routes `red` straight to `ended` ends the Run without a fixer, with `CI failed` in its report.
Waiting holds no Slot, because no agent works, and the Run keeps beating, so `resume` and `finish` apply to it as to any Run.
A PR with no check within `ci.grace` is a project without CI, told apart from pending CI by that period, and the Run ends `done` with `CiAbsent` journaled.
A Run that keeps its PR as a file, from a Spec folder in a project with no remote, skips the phase, and `ci: off` in the project file turns it off.

Before the owner is asked, the Run Stage `ci_fix`, with `workspace: integration`, works on red CI, up to `rounds.max` Rounds, and one out of Rounds or past a budget ends `exhausted`.
Each Round is a Fresh agent in a Workspace and Sandbox of its own at the Integration branch's tip, with a Brief of the failed jobs, their log and the diff against `base`.
The host commits its change with the Stage boundary rules of ADR 0009, and a guard by diff refuses a change of the CI config and a deleted, skipped or xfailed test.
The Stage routes `done` to the Run Gates it wants, as a Ticket Stage does: each is a node of a Gate kind that judges that commit in a Sandbox of the Run.
A Round that leaves its Gates moves the Integration branch, pushes it and waits for the CI of the new head, and a Round that fails leaves the branch alone and gives the next one its verdict.
The Run graph cannot know a project's commands, so the default one has no Run Gate: the graph `tdd-review` adds the Gates of that Pipeline, and a project with others replaces the graph.

## Considered Options

- More or stricter Gates.
  Rejected because a Gate on the Sandbox's machine cannot see a difference between machines.
- Ending the Run `done` and leaving CI to the owner, as before.
  Rejected because finding and fixing the failure by hand is the work the factory exists to take off the owner.
- Rerunning a failed job to see whether it passes.
  Rejected because a flaky test is a defect to fix, not a failure to retry.
- Watching CI on `main` after the PR merges.
  Out of scope: the Run's work ends at its final PR.

## Consequences

- A Run lasts as long as the CI of its PR, and `factory run` holds the terminal for that time.
- The failed jobs' logs are an Artifact of the Run (`ci/failed-<n>.md`), so a later fixer Stage can read them.
