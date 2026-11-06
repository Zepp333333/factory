# A Run is a YAML graph of named Run nodes, in the format of a Pipeline

A Ticket goes through a Pipeline a project describes in YAML, but the Run around the Tickets was fixed in code: the Tickets, then the PR, then the CI of the PR.
So a Run is a YAML graph too, in the format of ADR 0004: a `start`, an `escalate_to` and named nodes, each with a `next` that maps every outcome to the next node.
A Run graph holds its nodes in one section `nodes`, because a node there says what it is by its `kind`, and the terminal node of the level is `ended`.
One loader and one check serve both levels, and each level has its closed set of node kinds.
The first Run node kinds are coarse: `tickets` is the loop over the Tickets under the Merge window, `open_pr` is the push and the PR, and `wait_ci` is the CI wait with the CI fixer and `ci_failed` inside.
A node that names a Role and no `kind` is a Run Stage, a Fresh agent with the settings of a Stage, and so far the Describer's `describe` is the only one: it takes what it leaves out from the `defaults` of the Ticket Pipeline, as the block `describer` of a Pipeline file did before it.
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
- A loop in a Run graph is a check error, because no Run node kind has a limit on its Rounds yet.
- The brake, `budget_spent`, `infra_failure`, `cancel` and `abort` stay outside the graph, as Run-wide interrupts.
- A Run freezes its Run graph with the rest of its Library, so `resume` walks the graph it began with.
- A Journal without Run node events replays as the default Run graph: its Run starts at the graph's start again, and `tickets`, `describe`, `open_pr` and `wait_ci` do again only what the Journal does not hold yet.
  A frozen Library without `runs/` takes that graph from the `factory` Layer, and `settings` without `run` name `default`.
- `factory finish` is no walk of the graph: it ends the Run and lands what merged, with the Describer if the graph has one, unless the graph has no `open_pr` node.
- The block `describer` of a Pipeline file and the kind `land` are gone, and `factory check` names what replaced them, so a Run that froze either refuses to resume, as #318 decided for a retired Operation kind; its Journal still reads in `factory status` and `factory stats`.
- The default Run graph has `describe`, so a project whose Pipeline had no Describer gets one, and a Run graph without the node keeps the Tracker's text.
- The Describer's Attempts end in its own events, which carry their cost and duration for `factory stats`, and its cost stays out of what the budget counts.
- The Tracker Operations `assign`, `label` and `comment` are node kinds of both levels, from one `TrackerAction`.
  A Run node acts on each Ticket the Run took or, with `target: spec`, on the Spec, and each action's result goes in the Journal per target, so `resume` does only those it lacks.
  Assignment is such a node and not a step of `RunStart`, so the default Run graph assigns nothing and a project opts in.
- `select` is a Run node: kind `all` takes what the Tracker read, and is what `default` starts with, and kind `filter` takes the Tickets by their labels.
  It writes the Artifact `selection.json`, and `TicketsSelected` carries what that holds, because an event carries all that `apply` needs; the Run's Tickets, its PR text and its Tracker Operations then hold only the Tickets it took.
  So a Tracker no longer refuses a blocker it did not read and reports it instead: `factory run` refuses it before the Run starts unless the graph holds a `filter`, and then the Ticket it blocks stays queued and the PR text says why.
  A graph selects at most once and before `tickets`, and `empty` ends the Run `done` before any Workspace is made.
- `factory library show run <name>` prints a Run graph and its Layer, to start a project's own copy from.
