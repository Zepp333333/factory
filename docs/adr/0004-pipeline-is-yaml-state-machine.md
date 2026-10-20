# A Pipeline is a YAML state machine of named Stages, Gates and Decisions

A Pipeline is described in YAML as a finite state machine.
Stages, Gates and Decisions are named nodes in three sections, and each has a `next` that maps every outcome to the next node.
Outcomes are closed: a Stage has `done` and `exhausted`, a Gate has `pass` and `fail`, and a Decision has its declared list plus an implicit `escalate`.
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
  The project file can change only the fields of a Stage or Decider, never of a Role.
- The prompt is a static file without templating, and the engine appends the path to the Brief itself.
- A Decider is set as `{kind: model | function | consilium | human, role, model}`, and a Decision with a model Decider declares `inputs` the same way a Stage does.
- Input sources are `spec`, `ticket`, `cause` (the artifact of the node whose outcome led here) and `{stage | gate | decision: name or list}`, where a list means the latest artifact among those listed.
- A Stage whose Attempts are used up leaves no artifact; the Round limit fires only after at least one finished Round.
- Resetting Rounds zeroes the limit counter but not the history: the artifacts of past Rounds stay addressable.
- In v1 a Run has one Pipeline, named in the project file; choosing by Ticket label can be added later by making `pipeline:` a map.
- `workspace: rw | ro` is temporary and depends on the decision about passing code; Gate parameters in `with:` are settled separately.
