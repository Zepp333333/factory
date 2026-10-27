# A Pipeline is a YAML state machine of named Stages, Gates and Decisions

A Pipeline is described in YAML as a finite state machine.
Stages, Gates and Decisions are named nodes in three sections, and each has a `next` that maps every outcome to the next node.
Outcomes are closed: a Stage has `done`, `failed` and `exhausted` at the Round limit, a Gate has `pass` and `fail`, and a Decision has its declared list plus an implicit `escalate`.
An unmapped `escalate` or `exhausted` goes to the Pipeline-level `escalate_to`, and the terminal nodes are `done` and `abandoned`.
A Pipeline knows nothing about the Target project: a separate project file sets the project's values, the Sandbox and node overrides through `${vars.*}` and `overrides`.
The round limit and the model switch count the Rounds of a single Stage (`rounds: {max, then}`, `escalate: {from_round, model}`).
A transition can reset the Round counter (`{to, reset_rounds}`), and the Orchestrator carries out the reset, not a Decider.
Before a Run starts, the engine checks the graph: every outcome has a route, and a required input exists on every path to its node.

The prototype, with a draft for a huge private C++ repo, the check and scenario runs, lived on the branch `prototype/pipeline-format` in the original repository and was not carried over.

## Considered Options

- An ordered list of steps with `on_fail: goto`.
  Rejected because the review loop, escalation and Decisions turn into special cases, and the check does not see the graph as a whole.
- A Python DSL.
  Rejected because the Pipeline stops being declarative and cannot be checked before the start without executing code.
- One self-contained file per Target project.
  Rejected because the same Pipeline would have to be copied into every project just for different build commands and Sandbox image.
- A shared named loop counter across several nodes.
  Rejected because a per-Stage counter is simpler to explain and check, and the limit in Superpowers also counts fix rounds.
- A human outcome with a number of extra Rounds.
  Rejected because the outcome stops being an item of a closed list and carries data.
- A required input that is checked only during the Run, with `escalate` when it is missing.
  Rejected because ADR 0003 requires catching such an error before the start; the prototype's check found a real one: if `build` fails all five Rounds, `ruling` is reached without a single review.

## Consequences

- The prompt, the output schema and the skills live on the Role; the model, `profile` (`project` or `hermetic` for `--setting-sources`), `workspace`, `executor`, `attempts` and `timeout` live on the Stage.
  The project file can change only the fields of a Stage, Gate or Decider, never of a Role.
  An override can replace a Stage's `role` field, that is pick another Role from `roles`, but not the fields of the Role itself (`prompt`, `output`, `skills`): a Role is shared by several Stages and belongs to the Pipeline.
- The prompt is a static file without templating, and the engine appends the path to the Brief itself.
- A Decider is set as `{kind: model | function | consilium | human, role, model}`, and a Decision with a model Decider declares `inputs` the same way a Stage does.
- Input sources are `spec`, `ticket`, `cause` (the artifact of the node whose outcome led here) and `{stage | gate | decision: name or list}`, where a list means the latest artifact among those listed.
- A Stage whose Attempts are used up leaves no artifact; the Round limit fires only after at least one finished Round.
- Resetting Rounds zeroes the limit counter but not the history: the artifacts of past Rounds stay addressable.
- Only a Stage with `rounds` has the outcome `exhausted`, and `rounds.then` is its route; without `then` it goes by `next` or to `escalate_to`.
- A Stage with no Rounds left passes on the `cause` the Ticket entered it with, so the node after `exhausted` sees the verdict of the last failure.
- `escalate.from_round` counts the same Rounds as the limit, so after `reset_rounds` the Stage starts again with its own model.
- `reset_rounds` on a route that the Stage returns to with no Decision in between is a check error, because such a loop would zero the limit on every lap.
- In v1 a Run has one Pipeline, named in the project file; choosing by Ticket label can be added later by making `pipeline:` a map.
- ADR 0009 fixes the meaning of `workspace: rw | ro`; ADR 0005 describes Gate parameters in `with:`.
- Among the route errors that `factory check` rejects, three rules need their reason explained.
  Stages that send each other around in a loop on `exhausted` are forbidden, because such a loop never ends: every Stage in it is already past its Round limit, and the Ticket never reaches a human; a Round reset on a route breaks the loop.
  `merged` leads only to `done`, because the Orchestrator moves only active Tickets, and a node after a merged Ticket would never run.
  A Pipeline with a Gate must declare `escalate_to`, because a lost Probe gives no verdict and escalates, and without `escalate_to` it has nowhere to go.
