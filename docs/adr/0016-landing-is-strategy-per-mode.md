# The landing mode is a strategy that plans the Run's PRs, moves a fix to the PR's head and says whether the Run may merge

The project file picks how a Run lands with `landing: single` or `landing: stack`.
`single` opens one PR from the Integration branch into the base.
`stack` opens one PR per merged Ticket, each into the branch of the layer below, and the top layer's branch is at the Integration branch's tip.

The mode was a string that three places of `landing.py` compared with `"stack"`: the Describer's texts, the opening of the PRs and the refusal of `merge_pr`.
Other modules that depend on the mode did not check it at all.
The CI fixer pushed its fix to the Integration branch in either mode, while in `stack` the PR whose CI the Run waits for has the top layer's branch as its head.
So the fix landed on a branch that no PR watches, CI never started, and the wait ran until `ci.timeout` (#335, Run `20261106-112240-18a9` on #333).

So a landing mode is a strategy, the `Landing`, with one implementation per mode.
Every module that depends on the mode asks the `Landing`, and none of them names a mode.

## The interface

A `Landing` answers four questions:

| Question | Member | `single` | `stack` |
| --- | --- | --- | --- |
| Which PRs does the Run open? | `plan()` | One PR from the Integration branch into the base, with every merged Ticket. | One PR per merged Ticket, in the order the Integration branch merged them, each into the layer below. |
| Which branch is the head of the PR the Run waits on? | `watched()` | The Integration branch. | The top layer's branch. |
| Where does a fix go? | `land_fix(fixed, tip)` | Moves the Integration branch to the fix and pushes it. | Moves the Integration branch and the top layer's branch to the fix and pushes the top layer's branch. |
| May the Run merge its PR? | `merge_refusal()` | Nothing, so `merge_pr` holds the PR to its own rules of CI and head. | A stack merges through its top PR, by hand. |

`plan()` returns a list of `PlannedPr`:

| Field | Meaning |
| --- | --- |
| `number` | 0 for the one PR of `single`, and the layer's number in `stack`. |
| `head`, `commit` | The branch the PR opens from and its commit. |
| `base`, `base_commit` | The branch the PR opens into and its commit, which the Describer diffs against. |
| `tickets` | The Tickets the PR lands, whose footer closes them. |
| `top` | Whether this is the PR the Run waits on, whose URL is the Run's PR. |

The `Landing` also gives `notes(pr, plan)`, the text the engine appends to a PR's body after the footer.
`stack` lists the layers there and asks for a merge bottom up, and `single` adds nothing.

## Who asks it

- The Run Stage `describe` writes one text per `PlannedPr`, from the diff between `base_commit` and `commit`.
- The node `open_pr` pushes and opens each `PlannedPr` in order, skips a head that the Journal holds as opened, and journals the PR with `top`.
- The Rounds of `ci_fix` and `address` land a fix that left its Run Gates through `land_fix`, and journal `IntegrationBranchPushed` with the branch that `land_fix` pushed.
- The node `wait_ci` waits for CI on the PR that `top` names.
  When the head of that PR is still not the commit it awaits once `grace` has passed, the wait ends `timeout` at once and journals both commits, so the Decision after it does not wait out `ci.timeout` first.
- The node `merge_pr` refuses with `merge_refusal()` before its own checks.
- `factory finish` lands what merged through the same `plan()`.

The `Landing` reaches the kinds through the `NodeTools` of ADR 0015.
`wiring` registers the implementations by name, and `config.load_project` accepts `landing:` from the names of that registry, as it accepts the kind of an adapter.
The implementations live in `run/landings.py`, with `PlannedPr`.

## The invariant of `stack`

The Integration branch and the top layer's branch stay on one commit after every fix.
So a `plan()` that a Run makes again after `resume` or a fix finds the top layer at the Integration branch's tip, as it did when the PRs opened.

Only the CI of the top PR is the Run's CI.
A fix reaches the top PR alone, so a lower PR may keep the red CI of a fault that a layer above fixed.
The `notes` of `stack` say that the top PR carries the fixes of the layers below it, so the owner reads the top PR's CI as the verdict of the stack.

## Considered Options

- Keep the string and fix #335 with one more `if` in the CI fixer.
  Rejected because the next module that depends on the mode would miss the check, as the CI fixer did.
- Push a fix into the lowest red layer and rebuild the layers above it.
  Rejected because it rewrites layers a reviewer may have read, runs CI again on each layer and needs a merge forward through the stack, while the stack merges through its top PR.
  A mode that keeps every layer green can be a third implementation later.
- Push a fix to the top layer's branch only.
  Rejected because the Integration branch and the PR's head would differ, and `describe`, `resume` and `factory finish` read the Integration branch.
- A Run node kind per mode, such as `open_pr_stack`.
  Rejected because the mode decides the Describer, the CI fixer and the merge too, so one node kind cannot hold it, and a project would need a Run graph per mode for what is one setting of the project file.
- Merge a stack now, by retargeting its top PR to the base and merging it.
  Rejected for this ADR because it needs its own checks of ancestry and the closing of the lower layers' Tickets on the code host.
  It is one change of the `stack` implementation, with its own Ticket.

## Consequences

- #335 cannot happen again, because the CI fixer pushes where the `Landing` says and the wait refuses a commit that never became the PR's head.
  An end-to-end test runs a `stack` Run with red CI on the top PR, checks that the fix becomes the head of the top PR and that the Run reads its CI, and fails on the code before this ADR.
- No module compares `project.landing` with a mode, and a third mode is one implementation and one line in `wiring`.
- `single` lands as it does today, which the existing tests of landing prove.
- The `stack` refusal of `merge_pr` keeps its meaning and moves into `merge_refusal()`.
- R12 of the review, the `grace` clock of `wait_ci` that counts from `CiStarted`, is a fix of its own and does not depend on this ADR.
