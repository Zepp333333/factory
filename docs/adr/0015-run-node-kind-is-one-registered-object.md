# A Run node kind is one registered object, and the Orchestrator dispatches through the registry

ADR 0014 made the Run a YAML graph, but it kept a closed set of node kinds in code, and each kind was spread over six modules:

- its outcomes and `with` fields in `RUN_NODE_KINDS` of `pipeline`;
- the parsing of its `with` in `pipeline_loader`;
- its order rules as `if`s in `pipeline_checks`;
- its executor as a method of the Orchestrator, in the `run_nodes` dict;
- its events in `core/events`;
- its state as fields of `RunMemory`.

Each layer of the stack #322-#333 that added a kind touched 14 to 16 modules.
The Orchestrator built every phase itself and sat at the ceiling of 20 imports, so the Planner went into `landing` and the wait for a Tracker event went into `ci_wait`, each into a module about something else.
A Run Stage named no kind at all.
The engine guessed its agent from flags, where `workspace: integration` meant the CI fixer, the same with `inputs` meant the Addresser, a Role with `selects` meant the Planner, and anything else meant the Describer, and the Orchestrator matched on those flags.
So the graph was configurable only inside the set the engine knew, and a new kind still meant editing the core.

So a node kind of a Run graph is one object, a `RunNodeKind`, and one registry holds every kind, as the registries of `wiring` hold the adapter Kinds of ADR 0012 and `ports/kind`.

## The kind

A `RunNodeKind` holds everything the engine knows of a kind:

| Member | What it says |
| --- | --- |
| `name` | The `kind:` a node names. |
| `agent` | Whether a node of the kind is a Run Stage, a Fresh agent with a Role and the settings of a Stage. |
| `requires` | The fields an agent kind's Role schema must require, as `RunAgent.requires` does today. |
| `parse(body, report)` | Reads the node's `with` into the kind's own settings and reports what it rejects, as `Kind.parse` does. |
| `outcomes(node)` | The node's closed outcomes, which may depend on its settings, as a `wait_tracker` ends in each event of its `on`. |
| `order` | The rules `factory check` holds a graph to, below. |
| `roles(node)` | The roles of the Tracker and the code host the node uses, which `factory check` refuses on a project whose adapters do not serve them. It is not called `needs`, which `RunNode` already names the kinds that ran before the node by. |
| `labels(node)` | The labels the node needs on the Tracker, which `factory doctor` looks for. |
| `holds_pr` | Whether a Run in such a node has its PR open, which `factory finish` reads. |
| `shape` | How `factory diagram` draws the node. |
| `make(node, tools)` | Builds the runner of the node from the Run's `NodeTools`. |

The `order` of a kind holds these rules:

| Rule | Meaning | Example |
| --- | --- | --- |
| `count` | How many nodes of the kind, or of its `group`, a graph may hold. | One `tickets` node exactly, and one node of group `select` at most. |
| `group` | A name that several kinds share for `count`, `after` and `before`. | `all`, `filter` and `plan` are of group `select`. |
| `after(node)` | Kinds or groups that have run on every path to the node. | `merge_pr` comes after `open_pr` and `wait_ci`, and a `wait_tracker` on the PR after `open_pr`. |
| `before` | Kinds or groups that may not have run on any path to the node. | The group `select` comes before `tickets`, and `describe` before `open_pr`. |
| `commits` | Whether the kind's `done` leaves a commit, so it may lead to a Run Gate. | `ci_fix` and `address`. |
| `bounded` | Whether the node may end a loop, as a Decision or a Stage with a limit on its Rounds does. | A Run Stage with `rounds`. |

`check_run_graph` then holds every node to the `order` of its kind and names no kind itself.

## The runner

A `NodeRunner` carries out one node:

- `step()` works the node until it ends, and returns its outcome, or None for a Run the owner aborted.
- `finish()` does what `factory finish` asks of the node, and does nothing by default.
  `describe` writes the PR text and `open_pr` opens the PR, as `Orchestrator.finish` does today.

A runner keeps nothing between two steps that the Journal does not hold, so the Orchestrator makes it again at each entry and after `resume`, and `step` does only what the Journal does not hold yet (ADR 0001, ADR 0014).
What the report at the end of the Run shows comes from `RunMemory`, so a `LandingFailed` carries the commands that finish the landing by hand, which `open_pr` returns today.

## Where it lives

- `pipeline/run_kinds.py` defines `RunNodeKind`, its `order` and the `NodeRunner` protocol, and imports only `core`.
  So `pipeline_loader`, `pipeline_checks` and `factory diagram` read kinds without importing `run`.
  `make` takes its tools as `Any` there, as `Kind.make` takes its settings.
- Each kind lives in one module of `run/nodes/`, which defines its `RunNodeKind`, its runner, its settings and the handlers of its events.
- `wiring` registers the kinds, as it registers adapter Kinds, and hands the registry to `config.load_project`, the loader, the checks, `factory diagram`, `factory doctor` and the Orchestrator.
- No other module names a kind.

| Module of `run/nodes/` | Kinds | Comes from |
| --- | --- | --- |
| `tickets.py` | `tickets` | The Ticket loop of the Orchestrator. |
| `select.py` | `all`, `filter` | `Orchestrator._select`, `_take`. |
| `plan.py` | `plan` | `Planner` of `landing`. |
| `describe.py` | `describe` | `Lander.describe`. |
| `open_pr.py` | `open_pr` | `Lander.open_pr`. |
| `wait_ci.py` | `wait_ci` | `CiWaiter`. |
| `ci_fix.py` | `ci_fix`, `address` | `CiFixer`. |
| `wait_tracker.py` | `wait_tracker` | `TrackerWaiter` of `ci_wait`. |
| `merge_pr.py` | `merge_pr` | `Lander.merge`. |
| `tracker.py` | `assign`, `label`, `comment` | `TrackerOperations.act_for_run`, from one `TrackerAction`. |
| `run_gate.py` | Run Gates | `CiFixer.run_gate`. |
| `run_decision.py` | Run Decisions | `CiDecision.ask` of `ci_decision`. |

A Run Gate keeps the `kind` of its Gate and a Run Decision keeps its `decider`, as ADR 0014 has them.
The registry holds their runners under `gate` and `decision`, so the Orchestrator finds every runner in the registry and has no `match`.

## The Orchestrator

The Orchestrator walks the graph.
It enters a node, makes its runner, steps it, journals `RunNodeEnded` and routes the outcome.
It keeps the Run-wide interrupts of ADR 0014: the brake, `budget_spent`, `cancel` and `abort`.

It builds the services that more than one kind uses once, and hands them to each kind in `NodeTools`:

- the `RunTools` and the `RunContext`;
- the `Inbox`, for the owner's Commands;
- `hold(seconds)`, which beats, shows the Run and takes Commands while a node waits;
- the delivery of events to the `TicketBoard`;
- the Landing of ADR 0016;
- `Moves`, `Decisions`, `Operations`, the Tracker's operations and inputs and `RunStart`, which the Ticket loop works with.

The Ticket loop is not their only user.
The `Inbox` cancels and overrides Tickets while any node waits, `RunStart` takes Tickets back on `resume` and gives them up on `finish`, and both go through `Moves`.
So these are built once with the other shared services, and one Run holds one instance of each.
The `Throttle` of the brake is built once too, because its pace counts from the usage it first saw.
A service that one kind uses alone, such as the `Probes` of the Ticket loop, is built by that kind.

## Run Stages

A Run Stage names its kind, as every other node does, next to its `role`:

```yaml
nodes:
  select:
    kind: plan
    role: planner
    next: {selected: build, empty: ended, failed: ended}

  describe:
    kind: describe
    role: describer
    next: {done: open_pr, failed: open_pr}

  ci_fix:
    kind: ci_fix
    role: ci-fixer
    rounds: {max: 2, then: ci_failed}
    next: {done: ci, failed: ci_fix}

  address:
    kind: address
    role: tracker-fixer
    inputs: [{source: pr_comments}]
    next: {done: ci, failed: ended}
```

- The kinds `describe`, `plan`, `ci_fix` and `address` are agent kinds.
- The kind says what the Stage commits and which inputs it takes, so `workspace: integration` and the Role's `selects` are gone.
- The Addresser is a kind of its own and no longer borrows the CI fixer's record.
  The agent kinds journal their Attempts through events that carry the agent kind, so a new agent kind needs no event of its own and the Journal tells an Addresser Round from a CI fix Round (R6 of the review on #331).
  The events of today keep their read handlers, so an old Journal still reads in `factory status` and `factory stats`.

## Events and state

- An event name stays a member of `Event` in `core/events`, because the names are the contract of the Journal.
  Its handlers stay next to its writer, which is now the kind's module.
- A kind keeps what only it reads in the slot of its node in `RunMemory.nodes`, keyed by the node's name, which its own `applies` handlers fill.
  So two `wait_tracker` nodes of one graph keep two slots, and a new kind adds no field to `RunMemory`.
- What another kind or the report reads stays a field of `RunMemory`: the Tickets and their selection, the PRs opened, the pushed head, the CI wait and the merge.

## Considered Options

- Keep the closed sets, with a table of kinds in each module.
  Rejected because a new kind still touches six modules, and the Orchestrator still imports one more phase per kind.
- Two registries joined by name, the kind's rules in `pipeline` and its runner in `run`.
  Rejected because a kind is then two places that can drift apart.
  One object whose `make` takes its tools loosely keeps the kind in one module, as `Kind` of `ports/kind` does for adapters.
- Raise the import ceiling of the Orchestrator.
  Rejected because the ceiling showed a real cause: the Orchestrator owned every phase, and code moved into the wrong modules to stay under it.
- Kinds that a Target project registers from its own repository.
  Rejected for the reasons of ADR 0005 and ADR 0014: a node may not run code from the project on the host, which holds the owner's credentials, and the check could not see it before the start.
- Let the Role say which agent it is, as `selects` did.
  Rejected because what a node does in the Run is the graph's statement, and one Role may serve more than one kind.
- Keep the flags of Run Stages and register only the kinds of RunNodes.
  Rejected because the `match` on flags is the same closed set for agents, and a later change would reshape the registry again.

## Consequences

- A new Run node kind is one module of `run/nodes/`, one line in `wiring` and the names of its events in `core/events`.
- `RUN_NODE_KINDS`, the kind constants and the `RunNodeKind` of `pipeline` leave that module, and the properties of `Pipeline` that name kinds, such as `describer_stage`, `planner`, `after_the_pr`, `run_nodes_of` and `leaves_tickets_out`, read the registry.
- `check_run_graph` checks every node against the `order` of its kind.
  The rules that ADR 0014 lists keep their meaning and their messages.
- `roles` is where a node says it uses a role, so the optional roles for the events and the merge of a code host (R4 of the review) become check errors, not runtime errors.
- The Orchestrator drops below its import ceiling, and the Planner and the wait for a Tracker event get modules of their own (R2, R9 and R10 of the review).
- The module count of `src/factory` grows by about twelve and the edges move into `run/nodes/`.
  The PR that lands the registry re-measures the graph with `make measure-coupling` and sets `MAX_EDGES`, `MAX_MEAN_CE` and the `PACKAGE_CE` of `run` from what it measures, with the reason in its description.
- A project's own Run graph or Role in the old form fails `factory check`, and the error names the kind that replaces it.
  A Library frozen by a Run that began before this ADR reads its Run Stages by the old flags, so `resume` walks the graph it began with, and only the loader holds that rule, for frozen Libraries.
- A test of `wiring` proves that every kind the shipped Run graphs name is registered, and each kind's module has tests of its own through the fake ports.
- The kinds of a Ticket Pipeline, its Gates and its Operations, keep their sets.
  The Tracker Operations are kinds of both levels, so the Ticket level reuses the module `tracker.py` through its `TrackerAction`.
