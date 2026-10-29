# A Run ends when CI on its final PR is green or the owner decides, and Gates cannot stand in for CI

A Run used to count its work as done once it opened its final PR.
Run `20261028-180813-b500` ended `done` with PR #137, and CI failed on that PR in two tests that depended on the machine's speed: `FakeClock` moves on every idle tick, and a slow runner made the Run last `1s` where the tests pinned `0s`.
The Gates had passed in the Sandbox on the same commit.

Gates run the same commands as CI, but on another machine, with another OS, speed and environment, so they cannot rule such a failure out.
Only the Target project's own CI on the PR's head commit tells whether the work is done.

So after `PullRequestOpened` the Run enters phase `ci` and waits for the checks of the pushed head commit through the `PullRequests` port of its Tracker: `gh pr checks` on GitHub, the MR's head pipeline on GitLab.
Checks of any other commit do not count.
Green CI ends the Run `done`.
Red CI, or CI that does not end within `ci.timeout`, opens the Run-level Decision `ci_failed` with the outcomes `recheck` and `accept`, answered through `factory decide <run> @run <outcome>` like `infra_failure` (ADR 0007).
Waiting holds no Slot, because no agent works, and the Run keeps beating, so `resume` and `finish` apply to it as to any Run.
A PR with no check within `ci.grace` is a project without CI, told apart from pending CI by that period, and the Run ends `done` with `CiAbsent` journaled.
A Tracker without CI, such as `LocalTracker`, skips the phase, and `ci: off` in the project file turns it off.

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
