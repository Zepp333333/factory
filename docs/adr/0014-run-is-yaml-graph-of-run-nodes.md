# A Run is a YAML graph of named Run nodes, in the format of a Pipeline

A Ticket goes through a Pipeline a project describes in YAML, but the Run around the Tickets was fixed in code: the Tickets, then the PR, then the CI of the PR.
So a Run is a YAML graph too, in the format of ADR 0004: a `start`, an `escalate_to` and named nodes, each with a `next` that maps every outcome to the next node.
A Run graph holds its nodes in one section `nodes`, because a node there says what it is by its `kind`, and the terminal node of the level is `ended`.
One loader and one check serve both levels, and each level has its closed set of node kinds.
The first Run node kinds are `tickets`, the loop over the Tickets under the Merge window, `open_pr`, the push and the PR, and `wait_ci`, the CI wait, which ends `green`, `red`, `absent` or `timeout`.
A node that names a Role and no `kind` is a Run Stage, a Fresh agent with the settings of a Stage: it takes what it leaves out from the `defaults` of the Ticket Pipeline, as the block of a Pipeline file did before it.
The Describer's `describe` is one, `ci_fix` with `workspace: integration` and `rounds` is another, which commits its fix (ADR 0011), and the Planner's `select` is the third.
A node of a Gate kind is a Run Gate, which judges the commit of a `ci_fix` Round in a Sandbox of the Run, and a node that names a `decider` is a Decision, which the owner answers, as `ci_failed` is.
A Run Stage's `inputs` hold only Tracker inputs, of which `pr_comments` needs a path from `open_pr` to the node, so `pr_comments` waits for a Run Stage other than these three: the Describer and the Planner come before `open_pr`, and `ci_fix` takes no `inputs`, because its Brief is the failed CI.
`open_pr` takes the text of `describe` from the Journal, which names the file of the text, so a kill between the two nodes loses nothing, and it takes the Tracker's own text when `describe` ended `failed` or the graph has none.

A Run graph is an item of the Library of ADR 0010, in `runs/` of each Layer, found by name from `project` down to `factory`, and replaced only whole.
The factory ships `runs/default.yaml`, which is the Run as it was before this ADR.
The project file names a Run graph with `run:`, which defaults to `default`.
The `tickets` node names the Ticket Pipeline in `with.pipeline`, and the shipped graphs take it from `${vars.pipeline}`, which the project file's `pipeline:` sets, so a project file from before this ADR runs unchanged.
`overrides` reach Run nodes through their section `nodes`, as they reach Stages through `stages`.

The Orchestrator walks the graph, and each Run node journals `RunNodeEntered` and `RunNodeEnded` with its outcome and the node it leads to.
`resume` continues from the Run node the Journal ends in.

## Considered Options

- Hooks on fixed phases.
  Rejected because a hook can add a step but cannot change the order, skip a phase or loop, and the check does not see the Run as a whole.
- A Python DSL.
  Rejected for the reason ADR 0004 gives for a Pipeline: it cannot be checked before the start without executing code, and a Run node may not run arbitrary code on the host, which holds the owner's credentials.
- More top-level fields of the Pipeline, as `describer` and `ci_fix` are.
  Rejected because each new behaviour of a Run stays a special case in the engine, the order stays fixed, and one Run graph could not reuse several Pipelines.
- A section per node kind, as a Pipeline has for Stages and Gates.
  Rejected because a Run graph mixes more kinds than a Pipeline, and the `kind` of a node already tells them apart.

## Consequences

- An invariant of the engine lives in the meaning of a node kind, and `factory check` rejects a graph that breaks the order it needs:
  a graph has one `tickets` node, its start reaches it, `describe` and `open_pr` are reached only after it, and `describe` comes before `open_pr`.
  A `wait_tracker` node waits for an event on the PR or the Spec, through the one port member `events`, which a webhook source can fill as another adapter, and ends in that event or `timeout`; one on the PR is reached only after an `open_pr` node.
  A `merge_pr` node is reached only after an `open_pr` and a `wait_ci` node, and merges only the head whose CI went green or has no CI to read, so a graph cannot route red CI into a merge.
  A `ci_fix` Stage, an Addresser and a Decision are reached only after a `wait_ci` node, and only the fixer's `done` or a Run Gate's `pass` leads to a Run Gate.
- A loop in a Run graph is a check error unless a Decision or a Stage with a limit on its Rounds ends it.
- The brake, `budget_spent`, `infra_failure`, `cancel` and `abort` stay outside the graph, as Run-wide interrupts.
- A Run freezes its Run graph with the rest of its Library, so `resume` walks the graph it began with.
- A Journal without Run node events replays as the default Run graph: its Run starts at the graph's start again, and `tickets`, `describe`, `open_pr` and `wait_ci` do again only what the Journal does not hold yet.
  A frozen Library without `runs/` takes that graph from the `factory` Layer, and `settings` without `run` name `default`.
- `factory finish` is no walk of the graph: it ends the Run and lands what merged, with the Describer if the graph has one, unless the graph has no `open_pr` node.
- The blocks `describer` and `ci_fix` of a Pipeline file and the kind `land` are gone, and `factory check` names what replaced them, so a Run that froze any of them refuses to resume, as #318 decided for a retired Operation kind; its Journal still reads in `factory status` and `factory stats`.
- The default Run graph has `ci_fix`, so a project whose Pipeline had no CI fixer gets one, and a project on `tdd-review` names `run: tdd-review` to keep its Gates on the fix.
- The default Run graph has `describe`, so a project whose Pipeline had no Describer gets one, and a Run graph without the node keeps the Tracker's text.
- The Describer's Attempts end in its own events, which carry their cost and duration for `factory stats`, and its cost stays out of what the budget counts.
- The Tracker Operations `assign`, `label` and `comment` are node kinds of both levels, from one `TrackerAction`.
  A Run node acts on each Ticket the Run took or, with `target: spec`, on the Spec, and each action's result goes in the Journal per target, so `resume` does only those it lacks.
  Assignment is such a node and not a step of `RunStart`, so the default Run graph assigns nothing and a project opts in.
- `select` is a Run node: kind `all` takes what the Tracker read, and is what `default` starts with, and kind `filter` takes the Tickets by their labels.
  It writes the Artifact `selection.json`, and `TicketsSelected` carries what that holds, because an event carries all that `apply` needs; the Run's Tickets, its PR text and its Tracker Operations then hold only the Tickets it took.
  So a Tracker no longer refuses a blocker it did not read and reports it instead: `factory run` refuses it before the Run starts unless the graph holds a `filter`, and then the Ticket it blocks stays queued and the PR text says why.
  A graph selects at most once and before `tickets`, and `empty` ends the Run `done` before any Workspace is made.
- `select` can be a Run Stage, when its Role says `selects: true`, as `planner` of the Library does and the shipped Run graph `planned` uses, so an agent takes the Tickets and their order.
  A Role says it in place of the node, because the engine must hold that Role's schema to what it reads, and a project's own Role can do the same.
  The engine refuses an output that names a Ticket outside those read, repeats one, leaves one unnamed or orders a Ticket before one it depends on, and that output spends an Attempt with its reason in the next Brief.
  `PlannerFinished` names the file of the output and `TicketsSelected` carries what it selected, so `resume` never asks again (ADR 0001), and a Run killed while the agent works loses that Attempt, which spends none.
  A Run graph has at most one Describer and selects at most once, so it may hold a Planner and a Describer.
- `factory library show run <name>` prints a Run graph and its Layer, to start a project's own copy from.
- `factory diagram` draws a Pipeline or a Run graph from its file, each file starts with that diagram as a comment block, and `factory check` fails on a block that is missing or differs.
- A Stage with `workspace: integration` that takes `inputs` is the Addresser, which works on the comments on the PR and holds the slot of the CI fixer, so one Run graph has at most one of each.
  The work on an existing PR that #294 asks for is to be a Run graph that starts at such a node; it needs a start without a `tickets` node, which this graph format does not allow yet.
