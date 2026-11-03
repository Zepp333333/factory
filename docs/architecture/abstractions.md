# Abstractions

## Selecting an adapter

`wiring` is the composition root and the only module that imports an adapter.
It holds `Machine` and `Registries`, one registry per port, and each registry maps a kind name to a `Kind` of `ports/kind`:

| Registry | Kinds | Picked by |
| --- | --- | --- |
| `executors` | `claude-code` | `executor` |
| `sandboxes` | `local`, `sbx` | `sandbox` |
| `workbenches` | `terminal`, `orca` | `workbench` |
| `trackers` | `github`, `gitlab`, `local` | the Spec link |
| `pull_requests` | `github`, `gitlab` | the git remotes, or `code_host` |

Adding a kind to a port takes one `register` call in `wiring`.
Every adapter is a module of the package `adapters/`, `LocalSandbox` and the terminal Workbenches included, and each port is a module of `ports/`.
Each adapter module defines its own `Kind`, such as `sbx.KIND` and `github.TRACKER`, so `wiring` casts nothing and holds no function of any adapter.

- A `Kind` is one contract for every port:
  - `fields` and `parse`: the fields of the port's block of the project file besides `kind`, read into the settings type of that kind (`SbxSettings` for `sbx`, `LocalSettings` for `local`), with each field it rejects reported.
  - `check(settings, context) -> Problems`: the errors that stop `factory check` and the start of a Run, and the warnings they show.
    The `CheckContext` holds the Target project's repository, its git remotes, the Run's `base` and the Machine's tools.
    `gitlab` checks there that `glab` serves the Spec, and warns when `base` is not the default branch that GitLab closes issues on.
  - `make(settings, tools)`: builds the adapter, with the `gh` and `glab` commands of the Machine.
  - `picks(link)`: the rule of a port picked by a link, whose kinds then take that link as their settings.
    A Tracker kind owns the Spec links it picks, as a `SpecLink` with the Ticket ids: `github` by its host and `gitlab` by its `/-/issues/` path, and the kind without one (`local`) takes the rest.
    A PullRequests kind serves the git remote URLs it picks.
- The project file picks each port by a block, `executor: {kind: claude-code}`, `sandbox: {kind: sbx, cpus: 4}`, `workbench: {kind: orca}` or `code_host: {kind: gitlab}`, or by the scalar `executor: claude-code`, which is the block `{kind: claude-code}`.
  `config` reads each into a `Choice` of the kind name and its settings, with `Machine.kinds` as `Kinds`, and rejects any other name, so `config` imports no adapter and names no field of any kind.
  A field that another kind owns gives `<port>.<field> applies only to kind <name>`, such as `sandbox.template applies only to kind sbx`.
  A Run has one Executor, so a Stage, Describer or Decider may set `executor` only to the project's.
- A Sandbox kind declares `enforces_egress`.
  A kind that lists `egress` among its fields but cannot enforce it makes `factory check` fail with `sandbox.egress cannot be enforced by kind <name>`, instead of ignoring the field.
- An Executor declares what it needs from a Sandbox in `needs()`, and the Sandbox only applies it.
  `Needs` lists the hosts it calls (`egress`), the `env` it reads, the `credentials` its proxy holds and the `agent` it runs as.
  `sbx create` takes that `agent`, `claude` for `ClaudeCodeExecutor`, and `SbxSandbox` refuses to create a Sandbox for an Executor that names none.
  `ClaudeCodeExecutor` declares `api.anthropic.com` and the `CLAUDE_CODE_OAUTH_TOKEN` placeholder that the `sbx` proxy replaces, so `sbx.py` names no Anthropic host and no Claude variable.
  A `Credential` names the service the proxy holds, and `SbxSandbox.check` refuses a machine without it.
  A Sandbox without a proxy, such as `local`, leaves the credential's variable to the host.
- The `sandbox` block of every kind takes `env`, a mapping of variable names to strings that every `exec` in the Sandbox carries, Probes included, which `config` reads into `Project.sandbox_env`.
  The factory's own `factory.yaml` sets `UV_PROJECT_ENVIRONMENT` there, so `sbx.py` names no Python tool.
  `factory check` rejects a name that is no variable name or starts with `FACTORY_`, a value that is no string, and an `env` that sets the variable of the Executor's credential.
- `Machine.sandbox_of(project)` makes the Sandbox and calls `Sandbox.require` with the Executor's `needs()` merged with the project's `env`.
  `SbxSandbox` merges them with its own `egress` and opens the policy for the union.
  `RunStarted` journals `sandbox_egress` and `sandbox_env`, the hosts and the variable names, never the values.
- Every key of the project file resolves through `config.Sources` into the Run settings: the built-in default of `BUILT_IN`, the project keys of the Factory config, the project file, and the invocation's `--set`, a later Source winning and merging into the blocks of the ones below.
  `load_project` takes the Sources and parses the result as it parsed the project file alone, so a bad `--set` or Factory config default gives the message of the project file key.
  `RunStarted` records the resolved keys as `settings`, without the values of `sandbox.env`, and names those unlike the project file alone in `settings_unlike_file`, which `factory status` shows.
  `resume`, `finish` and `clean` load the project with the recorded `settings` as the top Source, which replaces each key whole; a `RunStarted` without them loads from the Sources as they are.
- `executor` defaults to `claude-code`, so a project file with `executor: claude-code` and one without it run the same.
- Tests pick adapters only through the registries.
  `Machine.registries` defaults to `REGISTRIES`, and `tests/harness.py` copies them and registers its fakes under the names of the real kinds, so the harness checks the models of the Pipeline through the same `Kinds.checks` that a real kind gives.
  `tests/test_wiring.py` registers a fake kind of every port on the harness's registries and runs it through `factory check` and `factory run`.
- Every kind passes the Port contract of its port, one module per port in `tests/contracts/`.
  Each module runs every kind of its registry in `REGISTRIES`, and the Executor, Sandbox and Workbench modules also the kinds the harness registers in their place.
  `tests/contracts/hosts.py` puts fakes of `gh`, `glab`, `sbx`, `orca` and `claude` on the `PATH`, so each kind's own `make` reaches its fake, and a kind without an entry in its module's `FAKED` table fails.
  `docs/guide/07-adding-an-adapter.md` names the contract tests and the one registration a new kind takes.

## The ports and the tools of a Run

`Ports` in `ports/run_ports` bundles the Executor, Sandbox, Workbench and Clock that `wiring` picks for a Run.
`Machine.ports_of` builds it once, `RunTools` carries it to the Orchestrator, and the Orchestrator hands it to each phase that uses it.
It holds the ports only, so a module that takes it imports nothing about Attempts.
The Tracker and `PullRequests` come apart from it, because a Run picks them from the Spec link and the git remotes.

`RunTools` in `run_tools` are the tools of a Run that the Orchestrator builds its phases from, next to the `RunContext` that says which Run it is:
the Journal, the project, the `Ports`, the home, the `Display`, the `PullRequests`, the host git,
the parts of the Run that keep state of their own and that the phases must share, its `RunCustody`, its `MergeWindow` and its `Attempts`,
and the two phases that `Attempts` or more than one phase use, its `RunDecisions` and its `RunAgents`.
`append` journals an event and applies it to the Run state, and `beat` keeps the Run's heartbeat.
`RunTools.new` builds them, so no phase takes a callback of the Orchestrator.
`Append`, the type of `append`, and `TICK_SECONDS` live in `run_context`, which every phase imports already.
`Display` in `run_context` is the protocol of what the engine shows the owner through, `ring`, `prompt`, `show` and `tick`.
`RunView` in `views` implements it and `cli` hands it to `run_entry`, so no module of the engine imports a view, which `tests/test_layers.py` checks.
`RunContext.sandbox` names the Sandbox of a Ticket in the Run.

- Only the Orchestrator takes `(context, tools)`.
  Every other phase takes the `RunContext` and then, by name, only the tools it uses and the shared phases it uses:
  `Probes` takes `append`, the `Ports`, the host git, the `Isolation` and the `Moves`, and `RunDecisions` the Journal, `append` and the `Display`.
  A phase's constructor and its imports show its width again, and `tests/test_layers.py` checks that no phase but the Orchestrator takes `RunTools` and that each phase reads every tool it keeps.
- A phase that uses only a part of a tool takes that part: `Probes` and `Operations` take the `Isolation` of the `Attempts`, `Decisions` the `ring` of the `Display`, and a phase that reads only the Pipeline takes the Pipeline and not the project.
- The shared phases are `Moves`, `Decisions`, `Operations` and `Inbox`, which more than one phase uses.
  The Orchestrator builds each of them once and hands it on, so one Run holds one instance of each:
  `Moves` takes the `Decisions` and the `Operations`, `Inbox` the `Decisions` and the `Moves`, `Probes` and `RunStart` the `Moves`, and `CiWaiter` the `Inbox`.
  `tests/test_layers.py` checks that `orchestrator` is the one module that builds them.
- The Orchestrator builds the `CiFixer` of a Run that lands on a forge and hands it to `CiWaiter`, and `RunTools.forge` says whether there is one.
  `Lander` and `CiFixer` share the `RunAgents` of the `RunTools`, and `Inbox` and `Attempts` its `RunDecisions`, so one Run holds one of each.
  `Operations` still builds its `IssueFiling`, from the tools it uses itself.
- No phase and no other module may import more than 20 modules, which `tests/test_layers.py` checks since step 7 of `coupling.md`, so a phase that grows wide splits, as the Orchestrator split in step 5, instead of taking one more import.
  Since #250 the test also caps the import edges of the whole graph and its mean Ce, so a phase that splits into modules that each import a share of the old one adds no edges, and a wide import that moves from a hub to its neighbours shows.
- `Attempts` is one of the tools and takes the `RunContext`, its `AttemptTools`, its `Pace` and `ask_run`, which `RunTools.new` and the tests of the charge build alike.
  `AttemptTools` are the Journal, `append`, the project, the `Ports` and the host git, and the `Pace` the machine's cap of slots, the tick and `beat`.
  It opens the Run's Decision through `RunDecisions.ask`, which `RunTools.new` hands it.

`Moves` in `moves` takes a Ticket from node to node: its claim, the route of an outcome, the start of a Round, the end of a node whose Attempts all failed and the loss of an Attempt.
The Orchestrator, `Probes`, `RunStart` and the `Inbox` all move Tickets through it, so none of them calls back into another.
`Inbox` in `inbox` carries out the owner's Commands: `cancel`, `override`, `limits` and the answers to a Ticket's Decision, and hands an answer to the Run's Decision to `RunDecisions`.

## Entering a Run

`run_entry` is the one module of the engine that `cli` imports.
`new_run` resolves the base of a new Run, gets its id, freezes the Library and starts its `RunContext` fresh, and `replayed_run` rebuilds the `RunContext` of a Run that resumes or finishes from its events.
Both build the `RunTools` and return the Orchestrator, so `factory run`, `resume` and `finish` each call one function and know neither the order of the steps nor the phases.
`keep_awake` blocks sleep while the Run goes and journals the block.
`wiring` builds the rest that the CLI needs of the engine, the `Ports` with `Machine.ports_of` and the `Cleaner` of `factory clean` with `Machine.cleaner_of`.

## Landing a Run: the Tracker and PullRequests

The `Tracker` holds the Spec and the Tickets, and `PullRequests` lands the Run's Integration branch as a PR.
They are two ports because the Spec and the code can live on different hosts, such as a Spec in a folder with the code on GitHub.
`Tracker` has no `pull_requests()`.

- A PullRequests kind has a `picks` rule on a git remote URL, as `accepts` does for one repository, and a `make` that builds the `PullRequests` from that URL.
  `github` serves `github.com`, and `gitlab` serves a host whose name has the label `gitlab`, such as `gitlab.com`.
- `Machine.pull_requests_of` picks them from the git remotes of the Target project, not from the Tracker.
  A remote that points at the repository of the Spec wins, so a Run whose Spec and code share a host behaves as before.
  Otherwise `origin` wins, then the first remote by name that a kind serves.
- The project file field `code_host` names the kind when the remote URL does not tell, such as a self-hosted GitLab at `git.example.com`.
  The kind then reads the host and the project from the remote URL.
- With no remote that a kind serves, a Spec on GitHub or GitLab still lands in its own repository, so the landing fails and names the remote to add.
  A `LocalTracker` keeps the PR in a `pull_requests` folder of the Spec, through the PullRequests kind `folder`, whose `open` writes the PR as a file and whose `edit` rewrites its body.
- A kind tells the engine what its code host does, so no module of `run` checks the type of the PullRequests.
  `pushes` tells whether the PR lands through a git remote, which `resolve_base` fetches the base from and the landing pushes to; the folder pushes nothing and journals `PullRequestRecorded`.
  `ci` reads `absent` where no CI runs, as on the folder, so the Run ends without waiting for CI and without the CI fixer.
  `ci_config_paths` names the CI config of the code host, which `ci_guard` refuses in a CI fix: `.github/` for `github`, `.gitlab/` and `.gitlab-ci.yml` for `gitlab`.
  `open` takes the number of a layer of a stack, which starts the name of the folder's file.
  The project file cannot name `folder` as `code_host`, because it serves no git remote.
- `factory check` reports a project whose git remotes no kind serves and names `code_host`.
- The Trackers keep the rest of the host: the CLI preflight in the `check` of their kind, the closing keywords, and the comments and labels.
  `SpecSource.code_url` returns the repository the Spec lives in, or None for a folder, and `SpecSource.pull_request_folder` the path of the folder that keeps the PR, or None.
- Ticket and issue ids are opaque text that only the Tracker kind reads.
  `factory run --tickets` splits its ids at commas into `SpecLink.tickets`, and the `check` of the Tracker kind refuses ids it cannot take, as `github` and `gitlab` refuse what is no issue number, while the folder's `read_spec` refuses an id with no Ticket file.
  `Issue.id` and the return of `create_issue` are text, and a Journal whose `IssueFiled` wrote an integer `number` still replays.
  `Closing.reference` shows an id in a text, `#12` on GitHub and GitLab and the bare id in a folder, which the footers, the issue list of the final PR, the commit that links filed issues, the Run tree and `factory status` use, so no module outside `adapters/` tells an issue number from a folder id.
  A `#new:<key>` placeholder in code and the Operation details of `factory status --timeline` still write `#<id>` for every Tracker.
- The Tracker serves its consumers through four roles of `ports/tracker`, as ADR 0012 decides.
  `SpecSource` reads the Spec for `factory run` and tells `Machine.pull_requests_of` where it lives, `Closing` writes the PR text, the footer, the defused text and the references, `TicketBoard` comments and labels for the Projection, and `IssueDesk` finds and files issues for `file_issues`.
  A kind's `make` returns a `Tracker` that holds the roles it serves, where `board` and `desk` may be None.
  The `RunContext` holds the `Closing`, and the `RunTools` hold the `TicketBoard` and the `IssueDesk`, which `Operations` hands to `IssueFiling`, so no consumer takes the whole Tracker.
  A Run without a `TicketBoard` projects nothing, and `Machine.problems` refuses a Pipeline whose Operation `file_issues` needs an `IssueDesk` the Tracker does not serve, at `factory check` and at the start of `factory run`.
- `LocalTracker` and `PullRequestFolder` live in `adapters/local_tracker`, so `ports/` holds protocols and value types only, which `tests/test_layers.py` checks.
  The help of `factory run` and `factory check` names the Tracker kinds of the registry.
- An Executor writes the Attempt events of `ports/executor` to `events.ndjson` next to the agent's raw stream, as ADR 0013 decides.
  `live.Silence` counts their `progress`, `render` renders them, and `Workbench.open_attempt` takes their file and no Executor, so no module outside `adapters/` reads a raw stream.
  `wiring.events_of_old_attempt` has `claude_code` normalize the raw stream of an Attempt from before them, once, when the renderer first reads it.
- An Executor owns its model names and efforts: `default_model` is what an Attempt runs when the Pipeline names none, `model_problem` tells why a name cannot run, and `effort_problem` tells why an effort cannot run.
  `pipeline_loader` declares the three as the protocol `Checks`, which the Executor implements, takes the Executors as `Kinds.checks` and checks every `model` and `effort` of the Pipeline, including those of `escalate`, the Describer and Deciders, at `factory check`.

## The settings of a Fresh agent

`AgentSettings` in `pipeline` holds what a Fresh agent runs with: `model`, `effort`, `profile`, `attempts`, `timeout`, `stall` and `executor`, and a Stage's `escalate`.
It is declared once and travels whole:

- `pipeline_loader` reads every block of a Fresh agent (`defaults`, a Stage, a Decider, `describer`, `ci_fix`) into it, and the field lists of those blocks come from `dataclasses.fields(AgentSettings)` plus the fields each block adds.
- `Stage`, `Decider` and `RunAgentSpec` hold it as `settings`, and `AgentWork` is a Role, the settings and the node's inputs.
- `for_round(counted)` applies `escalate`, so an Attempt runs with the settings of its Round.
- `AttemptRequest` carries the settings of the Round to the Executor, and only the adapter reads the fields it passes to its agent.
- `fingerprint_of` in `fingerprint` turns them into the flat `model` and `effort` of `AttemptStarted`, and `metrics_memory.FINGERPRINT` and `stats.GROUPS` take their keys from it, so old Journals still read.

A new setting is a field of `AgentSettings`, its parse in `pipeline_loader.PipelineLoader._settings` and its use in the adapter.

## The Run-level agent

A Run-level agent is a Fresh agent that no Ticket owns: the Describer, which writes the PR text, and the CI fixer.
They are two uses of one concept, and no module handles them in two branches that do the same thing:

- `RunAgent` in `run_agent` is what the factory knows of one by its name: the slot `/<name>`, the Sandbox, branch and Workspace it works in, the event that starts its work and the `ends` that stop it, the events of the clean-up of its Sandbox, the phase `run_view` shows while it works, the fields its Role must require and the fields its block adds.
  `RUN_AGENTS` lists the two.
- `RunAgentSpec` in `pipeline` is what the Pipeline sets of one: its Role, its settings, and the `rounds` and `gates` of the CI fixer.
  `Pipeline.run_agents` holds them by name, and `pipeline_loader` reads each block through one `_run_agent` loader.
- `pipeline_checks.check_roles` takes the list of the Pipeline's RunAgentSpecs and checks each Role against what its RunAgent requires.
- `RunAgents` in `run_agents` gives one its Workspace and Sandbox, runs its Attempts with `work_apart`, which waits for its slot and journals the start of each, and cleans up after it with its events.
  `landing` and `ci_fix` say what each does there.
- `RunDir.run_agent(name, round)` is the directory of its Artifacts, `run_view` finds the agent at work from its events, and `journal` releases its slot on its `ends`.

The Journal keeps the events of each under their own names, `DescriberStarted` and `CiFixStarted` and the rest, so a Run from before the concept reads as it did.
A new Run-level agent is a row of `RUN_AGENTS`, the `RunAgentSpec` fields it adds, its events and a module of its own.

## Reading the Journal

The Journal's rows are plain JSON with an event name, and three readers fold them: `run_state.apply` into the Run state, `timeline` into the lines of `factory status`, and `metrics.run_metrics` into the numbers of `factory stats`.
None of them lists the events.
Each takes the handler of an event from its registry in `events`:

| Registry | Reader | Memory |
| --- | --- | --- |
| `applies` | `run_state.apply` | `RunMemory` |
| `times` | `timeline` | `TimelineMemory` |
| `measures` | `run_metrics` | `MetricsMemory` |

- The memories live in `journal`: `RunMemory` in `run_state`, `TimelineMemory` in `timeline_memory` and `MetricsMemory` in `metrics_memory`.
  A writer of `run` imports the memory its handler folds into from there, and `timeline` and `metrics` in `views` only loop over the registry and render or sum up the memory, so the engine imports no view.
- A handler takes `(memory, payload)` and nothing else; the time of the event is `memory.at`, and the Run's Pipeline is `RunMemory.pipeline`.
- The module that writes an event registers its handlers next to the code that writes it, with `@applies(Event.X)`, and an event that tells a reader nothing registers `nothing`.
- A `Registry` takes one handler per event and raises on a second one, so two writers cannot both say what one event does, and the clash fails at import.
- `measures.skip` leaves an event out of the metrics, as `custody` does for `Heartbeat`.
- Every event has an `applies` handler, and the readers pass over an event without one, such as one of a newer factory.

A new event is a member of `Event` and its handlers in the module that writes it, and no reader changes.
`journal` keeps its own map from an event to the projection tables it writes, and `run_view` still matches on the events it reads.

## Open problems

None.
