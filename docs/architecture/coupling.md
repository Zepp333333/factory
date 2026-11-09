# Coupling and cohesion of `src/factory`

A baseline of how tightly the modules of `src/factory` hang together, taken before the refactor of the package structure.
`make measure-coupling` prints the numbers from the imports and the git history, and `tools/measure/coupling.py` explains each column.
Numbers are from `main` at `b42ea43`, after PRs #225, #226 and #227 landed.
The import graph and the git history are re-measured at `e7c95e5`, where step 7 starts, after the other six steps landed,
and the import graph again after the follow-ups #247, #248, #249 and #250 under Follow-ups.
Since step 6 `tools/measure/coupling.py` names a module by its file name in whichever package it sits, so the history before and after the move counts the same modules.

## Why this document exists

Two small, self-contained features each touched a large share of the code:

| PR | Feature | Files | Of them `src/factory` | New modules |
| --- | --- | --- | --- | --- |
| #225 | `effort` next to `model` on Pipeline nodes, passed as `--effort` | 23 | 9 | 0 |
| #226 | Run-level Stage `ci_fix`, a fixer for red CI on the final PR | 36 | 19 | 2 |

A field on a Pipeline node touched 9 of 56 modules.
A new Run-level agent touched 17 existing modules besides its own two.
The sections below say where that spread comes from, which of it the module graph shows and which of it it hides.

## What the import graph says

| Measure | Value |
| --- | --- |
| Modules | 73, `cli` counted as one |
| Import edges | 455, `tests/test_layers.py` caps them since #250 |
| Density | 0.09 |
| Mean fan-out (Ce) | 6.23, `tests/test_layers.py` caps it since #250 |
| Cycles | 0, `tests/test_layers.py` forbids them |
| Modules with Ce ≥ 10 | 17 |
| Modules with Ca ≥ 10 | 17 |

At the level of modules the coupling is moderate.
One module in ten pairs is joined, there are no cycles, and the layer rules in `docs/architecture/dependencies.md` hold.
The graph is not what makes a small feature wide.

The hubs are few and known:

| Module | Ca | Ce | Names out | Lines | Role in the graph |
| --- | --- | --- | --- | --- | --- |
| `orchestrator` | 1 | 20 | 34 | 344 | the loop, the claims, the brake, the phases it builds once and the dispatch to them |
| `cli` | 0 | 20 | 53 | 644 | the commands, which enter the engine through `run_entry` only |
| `attempt` | 5 | 20 | 49 | 501 | the charge of an Attempt, imported by `run_tools` and the four phases that take the `Attempts` |
| `config` | 14 | 8 | 19 | 221 | the project file, with the Pipeline file read by `pipeline_loader` |
| `pipeline_loader` | 2 | 9 | 26 | 593 | the Pipeline loader and what the project file shares with it |
| `run_start` | 2 | 20 | 30 | 265 | a phase with 4 unrelated groups of definitions, at the ceiling since it names its tools |
| `events` | 29 | 0 | 80 | 145 | the enum and the registries of handlers, which every writer and reader of the Journal imports |
| `states` | 22 | 0 | 44 | 28 | 10 constants with nothing joining them |
| `pipeline` | 23 | 1 | 68 | 258 | the domain model, 68 names taken by 23 modules |
| `tracker` | 16 | 2 | 53 | 293 | Tracker, PullRequests and CI status in one port |
| `run_context`, `run_tools` | 16, 2 | 9, 14 | 12, 20 | 73, 77 | the first argument of every phase, and the tools only the Orchestrator takes whole |

Each package imports these modules from outside it, which `tests/test_layers.py` caps since step 7:

| Package | Modules | Share | Ce |
| --- | --- | --- | --- |
| `core` | 10 | 14% | 0 |
| `pipeline` | 7 | 10% | 5 |
| `ports` | 5 | 7% | 5 |
| `adapters` | 7 | 10% | 12 |
| `journal` | 10 | 14% | 10 |
| `views` | 4 | 6% | 11 |
| `run` | 23 | 32% | 30 |

The widest edges are `pipeline_loader -> pipeline` with 16 names and `pipeline_checks -> pipeline` with 12.
The three modules are one concern, the Pipeline model and its loading and checking, split by layer into three files.

## What the git history says

Over the last 137 commits that touch `src/factory`:

| Measure | Value |
| --- | --- |
| Modules per commit, median | 3 |
| Modules per commit, p90 | 11 |
| Commits that touch 8 or more modules | 29 of 137 |
| Commits that touch `orchestrator` | 76 |
| Commits that touch `cli` | 54 |
| Commits that touch `config` | 49 |

The pairs that change together most, with the share of their commits they share (Jaccard):

| Pair | Together | Jaccard |
| --- | --- | --- |
| `cli` + `journal` | 23 | 0.40 |
| `cli` + `orchestrator` | 38 | 0.41 |
| `journal` + `tracker` | 13 | 0.41 |
| `config` + `orchestrator` | 36 | 0.40 |
| `config` + `pipeline` | 22 | 0.42 |
| `github` + `tracker` | 13 | 0.59 |
| `config` + `git` | 20 | 0.36 |

`github` + `tracker` is the port and its adapter, and that pair is expected.
The rest are modules that the import graph keeps apart but that a feature cannot change one without the other.
`orchestrator` is in more than half of all commits.
The steps of the plan are in this history too, and each of them is wide on purpose: step 6 moved 68 modules, and steps 3, 4 and 5 touched 14, 20 and 21.

## What makes a small feature wide

The module graph is layered horizontally, foundation to CLI, and a feature is a vertical slice through every layer.
Each layer restates the feature's data in its own words, so the feature lands in each of them.
Four mechanisms do most of it.

### 1. The settings of an agent are copied field by field

`model`, `profile`, `attempts`, `timeout`, `stall` and `executor` are declared as separate fields in each place they pass through:

| Place | Module |
| --- | --- |
| `_Settings` | `config` |
| `SETTINGS_FIELDS`, `DESCRIBER_FIELDS`, `CI_FIX_FIELDS`, `DECIDER_FIELDS`, `ESCALATE_FIELDS` | `config` |
| `AgentWork`, `Stage`, `Decider`, `Describer`, `CiFix`, `Escalate` | `pipeline` |
| `AttemptRequest` | `executor` |
| the fingerprint dict | `fingerprint` |
| `FINGERPRINT` | `metrics` |
| `GROUPS` | `stats` |
| `ModelNames` | `config`, built in `wiring` |

PR #225 added `effort` to each of them, and that is its 9 modules.
Only two of the nine hold behaviour that is about `effort`: the `--effort` flag in `claude_code` and the check of the value in `config`.
The other seven carry a field they do not read.
There was no value type for "the settings a Fresh agent runs with" that travels whole from the Pipeline file to the Executor and into the Journal.
Step 2 adds it as `AgentSettings`.

### 2. Every feature registers in the same central tables

A feature that adds an event writes the name into `Event` and then teaches each reader of the Journal about it:

| Reader | Distinct events it matches |
| --- | --- |
| `run_state.apply` | 35 |
| `journal` | 27 |
| `metrics` | 21 |
| `orchestrator` | 19 |
| `attempt` | 13 |
| `timeline` | 11 |
| `run_view` | 11 |
| `ci_fix` | 11 |
| `operations` | 9 |
| `run_start` | 8 |

The same holds for `artifacts`, which has one method per directory layout, for the field tuples of `config`, and for the slot names of `journal`.
PR #226 added 8 events, and the events then took it into `run_state`, `run_view`, `timeline`, `journal` and `artifacts`.
Each table is cohesive by mechanism, every one of them replays events, and not by feature, so every feature cuts across all of them.

Since step 4, `run_state.apply`, `timeline` and `metrics` match no event: each folds the events through a registry of handlers in `events`, which the writers of the events fill.
`journal` and `run_view` still match theirs.
Since ADR 0015, a kind of Run node is one registered object in a module of `run/nodes/`, and the loader, `factory check` and `factory diagram` read its parse, outcomes and order from the registry, where each of them kept a table of kinds before.

### 3. A Run-level agent is not a concept, it is a copy

The Describer is an agent that no Ticket owns.
It has its own slot, its own five events (`DescriberStarted`, `DescriberFailed`, `DescriberSandboxRemoved`, `DescriberSandboxLeftBehind`, `DescriberCleanupFailed`), its own branch in `attempt`, `landing`, `pipeline_checks`, `config`, `run_view`, `fingerprint` and `artifacts`.
13 modules name it.
The CI fixer of PR #226 is the second such agent, and it copies the pattern: `CI_FIX_SLOT`, six `CiFix*` events, a `CiFix` model next to `Describer`, a `_ci_fix` loader next to `_describer`, a `ci_fix` phase in `run_view` next to `describe`.
The PR generalised one piece, `Attempts.work_apart`, and copied the rest, so 12 modules name the CI fixer.
A third Run-level agent will cost the same 17 modules again.

### 4. The hubs grow with every feature

- `orchestrator` builds every phase and passes each its tools one by one.
  PR #226 touched it only to pass three more constructor arguments to `CiWaiter`.
- `cli` is a second composition root.
  It builds `Ports`, resolves the base, freezes the Library and builds `RunContext`, so most engine features reach it.
- `config` loads the project file, loads the Pipeline file and validates both, 796 lines and 58 definitions.
  Every field of the Pipeline schema is a change to it.

Since step 5, the Orchestrator builds each phase from the `RunContext` and the `RunTools` and nothing else, `cli` calls one function of `run_entry` to start, resume or finish a Run, and `config` reads only the project file.

## Cohesion

The column `components` of the report counts the groups of top-level definitions that name each other inside a module.
Most modules are one group, because most modules are one class with its helpers.
The low-cohesion modules are the small bags of constants and the phase modules that gather unrelated helpers:

| Module | Definitions | Components | Singletons |
| --- | --- | --- | --- |
| `states` | 10 | 10 | 10 |
| `names` | 5 | 5 | 5 |
| `run_start` | 5 | 4 | 3 |
| `question` | 8 | 4 | 3 |
| `tracker_cli` | 7 | 4 | 2 |
| `pipeline` | 23 | 3 | 2 |
| `deciders` | 15 | 3 | 1 |

The bigger cohesion problem is the one the import graph cannot count.
`run_view` is one connected component that knows every feature of the engine.
`run_state`, `metrics` and `timeline` knew every feature too until step 4, and now hold the memory of their reader and the loop over its registry.
`attempt` is one component too, and since step 1 it holds only the charge of an Attempt: `Ports` lived in `run_context` and lives in `ports` since #249, and the three modules that import `attempt` run Attempts.

## Goals of the refactor

- A feature of the size of #225 or #226 lands in the modules that own its behaviour and in no other.
  The guard is the measure above: p90 of modules per commit from 9 to 5 or fewer, and no pair outside port and adapter with Jaccard above 0.4.
- The package tree says what the layers table in `dependencies.md` says today, so a reader sees the structure in `ls` and the layer test guards the tree, not a hand-kept list.
- Each module has one reason to change (SRP), and a new kind of node, event or Run-level agent is an addition and not an edit of every reader (OCP).
- Data travels as value types, not as fields copied by hand.

## Plan

Seven steps, each one PR or a short series of PRs that keeps `make test` green and changes no behaviour a user sees.
The order matters.
The value types and the Run-level agent come first because they shrink the hubs, and the package tree comes last because moving files before the seams are right moves the problem with them.
Steps 1, 2 and 4 touch the lines of PRs #225 and #226, so the plan starts on the `main` that holds both.

Every step ends the same way:

- `make check` passes, and no test outside `tests/test_layers.py` changes except for import paths.
- `make measure-coupling` runs, and the numbers of this document are updated in the same PR.
- `docs/architecture/dependencies.md` and `abstractions.md` say what the step changed, and `CONTEXT.md` gets a term when the step names one.

### Step 0: freeze the baseline

| Field | Value |
| --- | --- |
| Scope | this document and `tools/measure/coupling.py` |
| Touches | `docs/architecture`, `tools/measure`, `Makefile` |
| Done when | the numbers above are from `main` with #225 and #226 merged |
| Status | done, measured at `b42ea43` |

Re-run the measure after the two PRs land and replace the numbers of the tables above, so each later step compares against the state it starts from.

### Step 1: `Ports` out of `attempt`

| Field | Value |
| --- | --- |
| Scope | the bundle of Executor, Sandbox, Workbench and Clock moves to `run_context` |
| Touches | `attempt`, `run_context`, the ten importers of `Ports`, `cli/common.py` among them |
| Done when | `attempt` has Ca ≤ 4, and only the modules that run an Attempt import it |

`Ports` is about a Run, not an Attempt, and `run_context` already holds the Run's frozen values.
The helpers that phases take from `attempt` (`resolve_input`, `schema_errors`, `gave_back`, `attempt_key`, `CleanupEvents`) move to the phase that uses them or to a small `inputs` module in the domain.
This is the cheapest step and it makes step 2 and step 5 smaller, so it goes first.

Status: done.
`gave_back` went to `run_decision`, `Ended` and `attempt_key` to `live`, and `resolve_input` and `schema_errors` to `inputs`, which takes `RunDir` and `LiveTicket` and so sits in the engine group, not in the domain.
`Probes` and `Operations` took `Attempts` only to reap, count and remove Sandboxes, so `reap`, `sandbox_gone_too_often`, `clean_up` and `CleanupEvents` went to `isolation`, and they take an `Isolation`.
`ci_wait` took `Attempts` only to build the `CiFixer`, so the Orchestrator builds the fixer and hands it to `CiWaiter`.
`attempt` went from Ca 11 to 4: `orchestrator`, `run_start`, `landing` and `ci_fix`.
`orchestrator` gained `ci_fix` and went from Ce 30 to 31.

### Step 2: `AgentSettings`

| Field | Value |
| --- | --- |
| Scope | one frozen value type for what a Fresh agent runs with |
| Touches | `pipeline`, `config`, `executor`, `fingerprint`, `metrics`, `stats`, `wiring`, `attempt`, `claude_code` |
| Done when | `model`, `effort`, `profile`, `attempts`, `timeout`, `stall` and `executor` are declared in one dataclass, and a new setting is a field, its parse and its use in the adapter |

1. Add `AgentSettings` to `pipeline` with the seven fields and `for_round(counted)`, which applies `Escalate`.
2. `Stage`, `Decider`, `Describer` and `CiFix` hold `settings: AgentSettings` and lose the copied fields.
   `AgentWork` becomes `Role` plus `AgentSettings` plus the Stage's inputs, or goes away if nothing else is left.
3. `_Settings` in `config` becomes `AgentSettings`, and `SETTINGS_FIELDS`, `DESCRIBER_FIELDS`, `DECIDER_FIELDS`, `ESCALATE_FIELDS` and `CI_FIX_FIELDS` are built from `dataclasses.fields(AgentSettings)` plus the fields each block adds.
4. `AttemptRequest` takes `settings: AgentSettings` instead of `model`, `effort` and `profile`.
5. The fingerprint dict and `metrics.FINGERPRINT` read the settings through one function `fingerprint_of(settings)` in `fingerprint`, and `stats.GROUPS` lists the fingerprint keys from that function.
6. `ModelNames` in `config` becomes a `Checks` protocol the Executor implements, with `model_problem` and `effort_problem`, so `wiring` passes the Executor and builds nothing.

The Journal keeps the flat keys `model` and `effort` in its payloads, so replay of old Runs still works and `tests/test_telemetry.py` does not change.

Status: done.
`AgentSettings` also holds `escalate`, which only a Stage sets, so `for_round` needs nothing else, and the field lists of `config` leave it out but for a Stage.
`AgentWork` stays as the name, Role, settings, inputs, `commits` and `outcomes` of an Attempt, and lost `escalate`, `model_for` and `effort_for`.
`commits` stays on `Stage`, because `workspace` says whether a Stage commits and is not a setting of the agent.
`ESCALATE_FIELDS` comes from `dataclasses.fields(Escalate)`.
`config` reads `workspace` in `_commits`, apart from `_settings`.
`ci_fix` and `landing` changed too, because they read `attempts` off the node, and now read it off its settings.
A new setting is a field of `AgentSettings`, its parse in `config` and its use in `claude_code`, three modules, or four when it joins the fingerprint through `fingerprint_of`.
The tests that build a `Stage` or an `AttemptRequest`, or read a setting off one, read it through `settings` now.
`pipeline` went from 279 lines to 260, and `config` takes 17 names from it, one more for `AgentSettings`.

### Step 3: the Run-level agent

| Field | Value |
| --- | --- |
| Scope | what the Describer and the CI fixer share becomes one module, `run_agent` |
| Touches | `attempt`, `landing`, `ci_fix`, `journal`, `events`, `run_state`, `run_view`, `timeline`, `artifacts`, `fingerprint`, `pipeline_checks`, `config` |
| Done when | `Describer` and `CiFixer` are two uses of `RunAgent`, and no module names either in a branch that the other also has |

A `RunAgent` is an agent that no Ticket owns, with a name (`describer`, `ci_fix`).
It owns, keyed by that name:

- the slot it holds in the Journal, in place of `DESCRIBER_SLOT` and `CI_FIX_SLOT`;
- the events `RunAgentStarted`, `RunAgentFinished`, `RunAgentSandboxRemoved`, `RunAgentSandboxLeftBehind` and `RunAgentCleanupFailed`, with the agent's name in the payload, in place of the ten `Describer*` and `CiFix*` events;
- `work_apart` from `attempt`, the wait for the slot, the start event and the cleanup of its Sandbox;
- its Artifacts directory under `artifacts.run_agent(name, round)`;
- the role check in `pipeline_checks`, which takes the list of Run-level agents of the Pipeline;
- the phase in `run_view`, which shows the name of the agent at work.

`Pipeline` holds `run_agents: dict[str, RunAgentSpec]` and `config` reads each block through one `_run_agent` loader, with what differs per agent (`body` for the Describer, `rounds` and `gates` for the fixer) in the spec.
Old Journals keep their `Describer*` and `CiFix*` events, and `run_state.apply` keeps reading them as the old names of the new events, so `factory status` and `factory stats` still read a Run from before this step.
This step touches many modules once, so that the next Run-level agent touches one.

Status: done.
`RunAgent` went to the foundation and not the engine, because `journal` releases its slot, `run_view` shows its phase and `config` and `pipeline_checks` read its block, and none of them may import the engine.
The engine half, `work_apart`, the wait for the slot, the start event, the Workspace and Sandbox and their clean-up, went to a module of its own, `run_agents`, with `RunAgents`.
`attempt` keeps the Attempt itself and gives `run_agents` `request` and `run_apart`.
The Journal keeps the events of each agent under their own names: the tests assert on `DescriberStarted`, `CiFixFinished` and the rest, and the Journal's rows are out of scope.
So `RunAgent` maps its kinds of event to those names, `started`, `ends`, `sandbox_removed`, `sandbox_left_behind` and `cleanup_failed`, and adds no `RunAgent*` event, and old Journals read as they did, which `tests/test_run_agent_replay.py` proves with a recorded one.
The Describer's `layer` and the CI fixer's Rounds, guard and Gates stay in `landing` and `ci_fix`, and `CiFixGateFinished`, `run_state.ci_fixes` and the CI fixer's line in `timeline` stay the CI fixer's alone.
`Pipeline.describer` stays as a property over `run_agents`, because tests read it.
`landing` and `ci_fix` import `run_agents` and no longer `attempt`, so `attempt` went from Ca 4 to 3.
`orchestrator` builds `RunAgents` for both and went from Ce 31 to 32.

### Step 4: Journal handling next to the writer

| Field | Value |
| --- | --- |
| Scope | the readers of the Journal stop listing every event |
| Touches | `events`, `run_state`, `metrics`, `timeline`, `run_view`, and every module that writes an event |
| Done when | adding an event adds no line to `run_state`, `timeline` or `metrics`, and `tests/test_layers.py` has a rule that says so |

The design choice is between typed event classes with methods and plain registration of handlers per event name.
The plan takes registration, because the Journal's rows stay plain JSON, which the ADRs and `factory stats` rely on, and because a handler is a function next to the code that writes the event.

1. `events` keeps the `Event` enum and adds three registries: `applies`, `times` and `measures`, each a mapping from an event name to a handler.
   A registry takes a handler once and rejects a second one for the same event.
2. `run_state.apply` becomes a loop over `applies`, `timeline` over `times` and `metrics` over `measures`.
   The `match` arms of today move, arm by arm, next to the code that writes the event: the Ticket arms into `attempt`, `probes`, `operations` and `decision`, the Run arms into `landing`, `ci_wait` and `run_agent`.
3. A handler takes `(memory, payload)` and nothing else, so the registries import nothing of the engine and the layer rules hold.
4. The `Describer*` and `CiFix*` events of old Journals register their handlers in `run_agent` too.

`orchestrator` and `attempt` keep matching on the events they react to live, because that is control flow and not a projection.
Their counts (19 and 13) fall in step 5.

Status: done.
`events` holds the registries `applies`, `times` and `measures`, each a `Registry` that takes one handler per event and raises at import on a second.
`run_state.apply`, `timeline` and `run_metrics` each fold the events through their registry, and `tests/test_layers.py` checks that the three name no member of `Event`.
Each reader keeps its memory as a type of its own, `RunMemory`, `TimelineMemory` and `MetricsMemory`, and the handlers write into it.
A handler takes `(memory, payload)`; the time of the event is a field of `TimelineMemory` and `MetricsMemory`, and the Run's Pipeline, which only the end of a `commits` Stage reads, is a field of `RunMemory` that `apply` sets.
The arms moved next to the writer of their event: `attempt`, `probes`, `operations`, `issue_filing`, `decision`, `landing`, `ci_wait`, `ci_fix`, `merge_window`, `run_start` and `orchestrator`, which writes `NodeEntered`, `TicketClaimed`, `LimitsChanged`, the throttle and `RunFinished`.
The handlers of `CiFixStarted` and `CiFixFinished` live in `ci_fix` and not in `run_agent`, because they write the CI fixer's own state and line, and `run_agent` is foundation and cannot name `RunMemory` or `TimelineMemory`.
`run_agent` registers the events of every Run-level agent's clean-up and the Describer's, which tell the Run state nothing.
Every event has an `applies` handler, `nothing` for those that change no state, and `tests/test_event_handlers.py` checks it once `wiring` has imported every writer.
`Heartbeat` is skipped by `measures`, in `custody`, so `metrics` names no event to leave the beats out.
`snapshot_blockers` went from `metrics` to `run_view`, its other reader, and both read the snapshot with `blockers_of`.
The writers import `run_state`, `timeline` and `metrics` for the memory types, so `orchestrator` went from Ce 32 to 34 and from 662 lines to 789, and `attempt` from Ce 18 to 20.

### Step 5: split the hubs

| Field | Value |
| --- | --- |
| Scope | `orchestrator`, `cli` and `config` each lose the part that is not theirs |
| Touches | `orchestrator`, `cli/run.py`, `cli/common.py`, `cli/resume.py`, `cli/finish.py`, `config`, new `inbox`, `run_entry`, `pipeline_loader` |
| Done when | `orchestrator` has Ce ≤ 20 and under 500 lines, `config` under 400 lines, and `cli` imports no module of the engine but `run_entry` |

1. `inbox`, a phase module for the Commands of the inbox (`cancel`, `override`, `limits`, the owner's answers), takes the 180 lines of `_cancel`, `_override`, `_limits` and `_carry_out_answer` out of `orchestrator`, as `run_decision` does for Decisions.
2. The callbacks that phases take from the Orchestrator (`_beat`, `_leave`, `_mounts`, `_lose_attempt`, `_apply_commands`) move into the modules that own their state, as `dependencies.md` already lists under open problems.
   The per-Run tools every phase takes (`append`, `git`, `attempts`, `context`) become one `RunTools` value next to `RunContext`, so a phase constructor takes two arguments.
3. `pipeline_loader` takes `_PipelineLoader` and the field tuples out of `config`, which keeps `load_project`, `Project` and `load_slots`.
4. `run_entry` builds a new Run (`resolve_base`, `freeze_library`, `new_run_id`, `RunContext.new`, the Orchestrator) and a replayed one from the project, the Spec and the ports, so `cli/run.py`, `cli/resume.py` and `cli/finish.py` each call one function.

Status: done.
`inbox` holds `Inbox` with the Commands of the inbox, and the handlers of `ConflictDropped`, `TicketCancelled`, `LimitsChanged` and the Command events.
`_leave`, `_enter`, `_conclude`, `_lose_attempt`, the claim of a Ticket and the mark of its Stage went to `Moves` in a new module `moves`, with the handlers of `TicketClaimed`, `NodeEntered` and the Round events, because `probes`, `run_start` and `inbox` move Tickets too.
`_mounts` went to `Isolation.mounts`, `_beat` to `RunTools.beat`, `_apply_commands` to the `Inbox` that `CiWaiter` builds, and the Gate branch of `_advance` to `Probes.step`.
`_give_up` and `_save_unsaved` of `factory finish` went to `run_start`, next to `stop_leftover`.
`RunTools` went to a module of its own, `run_tools`, and not to `run_context`, because it holds the shared `Attempts`, `MergeWindow` and `RunCustody`, and `attempt` imports `run_context`.
The Orchestrator takes `(context, tools)` too, and `run_entry` builds both.
`Attempts` is one of the tools and keeps its arguments, because `tests/test_attempt_charge.py` builds it; `run_entry` hands it `RunDecisions.ask`.
A phase builds the stateless phases it needs, as `Lander` builds `RunAgents` and `CiWaiter` builds `CiFixer`, so only the stateful parts travel in `RunTools`.
`CI_OUTCOMES` moved from `ci_wait` to `run_state`, so `run_decision` no longer imports `ci_wait`, and `ci_wait` can import `inbox` without a cycle.
`pipeline_loader` holds `PipelineLoader`, the field lists, `ConfigError`, `Kinds`, `Checks` and the collector of errors, and `config` re-exports `ConfigError` and `display_path`.
`Machine.ports_of` builds the `Ports` and `Machine.cleaner_of` the `Cleaner`, `run_entry.keep_awake` journals the block of sleep, and `sleep_block`, which imports nothing of the package, moved to the foundation, so `cli` imports no engine module but `run_entry`, which `tests/test_layers.py` checks.
`tests/test_layers.py` also checks that every phase takes `(context, tools)`.
The tests that read `TICK_SECONDS` or `RATE_LIMIT_PAUSE` from `orchestrator` read them from `run_tools` and `attempt`.
`orchestrator` went from Ce 34 to 19 and from 789 lines to 340, `cli` from Ce 24 to 20, and `config` from 794 lines to 220.

### Step 6: packages by concept

| Field | Value |
| --- | --- |
| Scope | the flat package folds into packages that match the groups of the layer test |
| Touches | every module, by `git mv` and import rewrite only |
| Done when | `ls src/factory` shows the layers, and `tests/test_layers.py` derives the group of a module from its package |

| Package | Modules of today |
| --- | --- |
| `factory/core/` | `clock`, `events`, `front_matter`, `git`, `names`, `sleep_block`, `spawn`, `states`, `values` |
| `factory/pipeline/` | `pipeline`, `pipeline_loader`, `pipeline_checks`, `gates`, `deciders`, `fingerprint`, `library_lookup`, `throttle` |
| `factory/ports/` | `executor`, `sandbox`, `tracker`, `workbench` |
| `factory/adapters/` | `claude_code`, `github`, `gitlab`, `sbx`, `tracker_cli`, `LocalSandbox`, `TerminalWorkbench`, `OrcaWorkbench` |
| `factory/journal/` | `journal`, `commands`, `custody`, `run_state`, `recovery`, `question`, `projection` |
| `factory/run/` | `orchestrator`, `run_context`, `run_tools`, `run_entry`, `run_start`, `attempt`, `run_agent`, `landing`, `ci_wait`, `ci_fix`, `inbox`, `moves`, `run_decision`, `decision`, `probes`, `operations`, `issue_filing`, `merge_window`, `live`, `clean` |
| `factory/views/` | `run_view`, `timeline`, `metrics`, `stats`, `artifacts` |
| `factory/` | `config`, `wiring`, `render`, `cli/` |

One PR per package, each a pure move, so a review reads the import rewrites and nothing else.
`LocalSandbox`, `TerminalWorkbench` and `OrcaWorkbench` move out of the ports into `adapters`, which closes the known exception of `dependencies.md` and lets the layer test drop `ADAPTER_CLASSES`.
`tests/test_layers.py` then keeps only the table of which package may import which, and reads the membership from the tree.

Status: done.
`ls src/factory` shows `core`, `pipeline`, `ports`, `adapters`, `journal`, `run`, `views` and `cli`, and `config`, `pipeline_loader`, `wiring` and `render` at the top.
Three modules sit apart from the table above, for reasons `dependencies.md` gives under Where a module sits:
`run_agent` stays in `core`, because `pipeline`, `pipeline_loader`, `journal` and `run_view` import it;
`pipeline_loader` sits at the top next to `config`, because it once read the `SandboxKind`s of `sandbox` and the ports import `pipeline`, and now that it reads no port it may move to `pipeline`;
`ci_guard`, which landed after the plan, is pure and read only by `ci_fix`, so it sits in `run`.
`run_tools`, `moves`, `inputs`, `isolation` and `run_agents`, which steps 1 to 5 added, sit in `run`.
`LocalSandbox` went to `adapters/local_sandbox` and `TerminalWorkbench` and `OrcaWorkbench` to `adapters/terminal_workbench`, so `ADAPTER_CLASSES` is gone, and `sandbox` went from 280 lines to 165 and `workbench` from 227 to 47.
`tests/test_layers.py` reads the group of a module from its package, or from its own name at the top of `src/factory`, and keeps `MAY_IMPORT` and nothing else by hand.
The rule that `journal`, `commands` and `custody` import only the foundation went with the storage group, and the three still import only `core` and each other.
The other numbers of the graph did not move, and the two new modules add two to the count and four edges.

### Step 7: guard

| Field | Value |
| --- | --- |
| Scope | the measure becomes a check |
| Touches | `tests/test_layers.py`, `tools/measure/coupling.py`, `AGENTS.md`, `.factory/prompts/describer.md` |
| Done when | CI fails on a module above the fan-out ceiling, and the co-change numbers are in the review of every PR that touches more than five modules |

- `tests/test_layers.py` takes a ceiling of Ce per module (20) and per package, so a hub cannot grow back.
- A PR that touches more than five modules of `src/factory` says in its description which step of this document explains the width, or what new seam it found.

Status: done.
`tests/test_layers.py` holds `MAX_CE`, the ceiling of 20 modules that one module may import, and `PACKAGE_CE`, the modules outside it that each package may import, set to the counts of the table under What the import graph says.
`cli`, `wiring` and `attempt` sit at the ceiling of 20, and `orchestrator` too since #247.
A test builds a module that imports 21 modules and a package one module above its ceiling and checks that both fail, and every package needs a ceiling.
`make measure-coupling` prints the Ce of each package next to its modules.
The width note is asked for twice: `AGENTS.md` asks it of a PR written by hand, and the Describer of this repository, in `.factory/prompts/describer.md`, writes it into the `Summary` of a Run's PR.
The co-change numbers stay in the description and not in a Gate, because the history of one PR is too short to measure.

### What the plan leaves alone

- The Journal's rows, the Artifacts layout and the CLI output, because the tests assert on them and the ADRs name them.
- The port protocols `Executor`, `Sandbox`, `Workbench`, `Tracker` and `PullRequests`, besides the move of `ModelNames` to the Executor.
- `tracker` as one port for Tracker, PullRequests and CI status.
  Splitting it is a separate decision with its own ADR, because two adapters would gain a third class each.

### Numbers after the plan

The exit criteria of #230 against `e7c95e5`, after all seven steps:

| Measure | Before the plan | After | Target | Met |
| --- | --- | --- | --- | --- |
| Modules per commit, p90 | 9 | 11 | ≤ 5 | no |
| Pairs with Jaccard > 0.4 outside port and adapter | 2 | 3 | 0 | no |
| `orchestrator` Ce | 30 | 19, 20 since #247 | ≤ 20 | yes |
| `attempt` Ca | 11 | 1, 5 since #248 | ≤ 4 | no, since #248 |
| `config` lines | 796 | 221 | < 400 | yes |
| Modules a new Pipeline setting touches | 9 | 3 | 3 | yes |
| Modules a new Run-level agent touches | 17 | 3 plus its own: `run_agent`, `events` and the phase that starts it | 1 plus its own | no |
| Modules a new event touches | 4 to 6 | 1 plus its writer: `events`, and `journal` or `run_view` when they show it | 1 plus its writer | yes |

Five targets were met after the plan, and four are since #248, which shows the phases that take the `Attempts` importing `attempt`; the ceiling of step 7 keeps `orchestrator` and every other hub at a Ce of 20 or less.
The two co-change targets are not met, and they cannot be yet: they read the history, and the history holds the commits from before the plan and the wide commits of the plan itself.
`cli` + `orchestrator`, `journal` + `tracker` and `config` + `pipeline` are joined by commits from before steps 2 and 5.
They become a measure of the new structure once the features that land after the plan fill the window of `make measure-coupling`.
A new Run-level agent touches `run_agent` for its kinds of event, `events` for their names and the phase that starts it, two modules more than the target, because the Journal keeps each agent's events under their own names.

## Follow-ups

The review of the stack #239-#245 found that the hubs shrank, but the import graph grew from 327 to 427 edges and the mean Ce from 5.6 to 6.2.

### #247: phases share one instance of each collaborator

The Orchestrator builds `Decisions`, `Operations`, `Moves` and `Inbox` once and hands them to the phases that use them:
`Moves` takes the `Decisions` and the `Operations`, `Inbox` the `Decisions` and the `Moves`, `Probes` and `RunStart` the `Moves`, and `CiWaiter` the `Inbox`.
Before, `Moves` was built in four places and `Decisions` in two, and the `Inbox` held two different `Decisions`.
`tests/test_layers.py` checks that each of the four is built in `orchestrator` alone, and that a phase takes `(context, tools)` and then only these shared phases.
`Attempts` takes four arguments instead of eleven: the `RunContext`, its `AttemptTools`, its `Pace` and `ask_run`, which `RunTools.new` and `tests/test_attempt_charge.py` build the same way.
`orchestrator` imports `decision` and `operations` now, and no longer `custody`, because `RunCustody.interval` gives the longest sleep that still beats in time.
The graph went from 427 edges to 428, and `orchestrator` from Ce 19 to 20, which is its ceiling.
The mean Ce stays at 6.2, and the git history is not re-measured, because it does not hold this change yet.

### #248: each phase takes only the Run tools it uses

Only the Orchestrator takes `RunTools` whole.
Every other phase takes the `RunContext` first and then, by name, the tools it uses and the shared phases it uses, so its width shows again in its constructor and its imports.
`tests/test_layers.py` checks that no phase but the Orchestrator takes `RunTools`, and that each phase reads every tool it keeps from its constructor.
The check found that `RunStart` kept the Journal and never read it.

- `RunTools` hold the one `RunDecisions` and the one `RunAgents` of the Run.
  `RunTools.new` builds the `RunDecisions` and hands its `ask` to the `Attempts`, so `run_entry` no longer points `ask_run` at a `RunDecisions` of its own, and the `Inbox` takes the same one.
  `Lander` and `CiFixer` take the same `RunAgents`, where each built one before.
- The Orchestrator builds the `CiFixer` of a Run on a forge, which `RunTools.forge` gives, and hands it to `CiWaiter`, which built it before.
  `orchestrator` imports `ci_fix` for it, and no longer `run_decision`, because `OWNER_ABORTED`, which only the Orchestrator reads, moved into it.
- `Append`, the type of `append`, and `TICK_SECONDS` moved to `run_context`, so no phase imports `run_tools`.
  The tests that read `TICK_SECONDS` read it from `run_context`.

The Ce of each phase, before and after:

| Phase | Before | After | What it imports now and did not before, or no longer |
| --- | --- | --- | --- |
| `orchestrator` | 20 | 20 | `ci_fix` in, `run_decision` out |
| `run_start` | 18 | 20 | `attempt`, `merge_window`, `custody` in, `run_tools` out |
| `decision` | 19 | 19 | `git` in, `run_tools` out |
| `ci_fix` | 19 | 19 | `config` in, `run_tools` out |
| `landing` | 17 | 17 | `config` in, `run_tools` out |
| `inbox` | 15 | 17 | `git`, `attempt`, `merge_window` in, `run_tools` out |
| `moves` | 13 | 17 | `config`, `git`, `journal`, `attempt`, `merge_window` in, `run_tools` out |
| `probes` | 13 | 14 | `git`, `isolation` in, `run_tools` out |
| `operations` | 14 | 13 | `run_tools` out |
| `ci_wait` | 12 | 13 | `journal`, `run_view` in, `run_tools` out |
| `run_agents` | 11 | 12 | `config`, `attempt` in, `run_tools` out |
| `run_decision` | 11 | 11 | `run_view` in, `run_tools` out |
| `issue_filing` | 9 | 8 | `run_tools` out |

`run_tools` went from Ca 14 to 2, `orchestrator` and `run_entry`, and from Ce 13 to 14, because it builds `RunDecisions` and `RunAgents`.
`attempt` went from Ca 1 to 5: `moves`, `inbox`, `run_start` and `run_agents` take the `Attempts` and import `attempt`, which they reached through `run_tools` before.
That breaks the target of `attempt` Ca ≤ 4 by one, and the graph now says what it hid.
The graph went from 428 edges to 437 and the mean Ce from 6.2 to 6.3, because the phases import the modules of the tools they take instead of the one `run_tools`.
`run_start` sits at the ceiling of 20 now, next to `orchestrator`.

### #249: the engine imports no view

No module of `run` imports from `views`, and the row of `run` in `MAY_IMPORT` of `tests/test_layers.py` no longer lists `views`.
Before, `run` imported `artifacts`, `metrics`, `timeline` and `run_view` from `views`, in 37 import edges.

- `TimelineMemory`, with its `Line` and `Entry` and `duration`, moved from `timeline` to `journal/timeline_memory`.
  `MetricsMemory`, with the numbers it folds into, `AttemptMetrics`, `TicketMetrics`, `GateMetrics` and `ChangeSize`, and the helpers the handlers call, `size`, `spend`, `attempt_metrics`, `blockers_of` and `FINGERPRINT`, moved from `metrics` to `journal/metrics_memory`.
  `timeline` keeps the loop over `times` and the rendering of the lines, and `metrics` keeps `RunMetrics` and `run_metrics`, the loop over `measures`.
  `tests/test_layers.py` checks that the two memories name no event either.
- `artifacts` moved from `views` to `journal`, which holds the Run's state, so `RunDir` and `write_once` sit next to the Journal.
- `Ports` moved from `run_context` to `ports/run_ports`.
- `run_context` holds `Display`, the protocol of what the engine shows the owner through: `ring`, `prompt`, `show` and `tick`.
  `RunView` implements it, and `cli` hands it to `run_entry` as before, so `run_tools`, `run_entry`, `ci_wait` and `run_decision` no longer import `run_view`.
- `RunContext.sandbox` names the Sandbox of a Ticket in the Run, so `attempt`, `run_start`, `decision` and `probes` no longer import `names`, and `operations` uses it but still imports `names` for `abandoned_ref`.
  Without it `attempt` and `run_start` would import 21 modules, because they import `run_ports` next to `run_context` now.

The Ce of each module that changed, before and after:

| Module | Before | After | What it imports now and did not before, or no longer |
| --- | --- | --- | --- |
| `ci_fix` | 19 | 20 | `run_ports` in |
| `moves` | 17 | 18 | `run_ports` in |
| `operations` | 13 | 14 | `run_ports` in |
| `run_agents` | 12 | 13 | `run_ports` in |
| `issue_filing` | 8 | 9 | `run_ports` in |
| `run_decision` | 11 | 10 | `run_view` out |
| `run_context` | 13 | 9 | `clock`, `executor`, `sandbox`, `workbench` out with `Ports` |
| `timeline` | 2 | 3 | `timeline_memory` in |
| `stats` | 3 | 4 | `metrics_memory` in |

`attempt`, `run_start`, `decision` and `probes` stay at 20, 20, 19 and 14: `run_ports` in, `names` out.
`ci_wait`, `run_entry` and `run_tools` stay at 13, 12 and 14: `run_ports` in, `run_view` out.
`isolation` stays at 9: `run_ports` in, `run_context` out, which it imported for `Ports` alone.
In the other modules of `run` a module of `journal` replaced each module of `views`, one for one.

`run` still imports 30 modules from outside it, four of `journal` and `ports` in place of the four of `views`.
Three package ceilings went up by one, each for a module this Ticket asked to move:
`ports` from 4 to 5, because `run_ports` imports `clock`;
`journal` from 9 to 10, because `metrics_memory` imports `fingerprint` for the keys of the fingerprint, as `metrics` did;
and `views` from 9 to 10, because `timeline`, `metrics` and `stats` import the two memories from `journal`, while `views` no longer imports `tracker`, which `artifacts` took.
The graph went from 69 modules to 72 and from 437 edges to 444, and the mean Ce from 6.3 to 6.2.
Twelve modules of `run` import `run_ports` next to `run_context`, where `run_context` gave both before, `timeline`, `metrics` and `stats` import a memory from `journal`, and `metrics_memory` takes over the edge of `metrics` to `fingerprint`, which makes 15 edges more.
`RunContext.sandbox` saves four edges to `names` and `Display` four to `run_view`, so 7 are left.

### #250: CI caps the total import edges and the mean Ce

The ceilings of step 7 cap each module and each package, so coupling that spreads over many modules passed them:
the stack #239-#245 and the follow-ups took the graph from 327 edges to 444 while every hub shrank.
`tests/test_layers.py` holds `MAX_EDGES`, 444, and `MAX_MEAN_CE`, 6.25, the edges per module, and fails when the graph goes above either.
A test adds one edge to the graph and drops its modules that import nothing, and checks that each cap names the graph.
The rule of the other ceilings holds: a PR that sheds edges lowers the caps, and a PR that raises one says why, which `AGENTS.md` asks of a PR written by hand and `.factory/prompts/describer.md` of a Run's PR.

`make measure-coupling` prints the mean Ce with two decimals and the share of each package in the modules of the graph.
It no longer counts the empty `__init__` at the top of `src/factory` as a module, as `tests/test_layers.py` never did, so the graph has 71 modules, not 72, and its mean Ce is 6.25, where the report said 6.2.
The edges did not move.
`run` holds 23 of the 71 modules, 32%, and imports 30 from outside it, which is its ceiling.

### #259: Run settings resolve through their Sources

Step 1 of #258.
`config` resolves every key of the project file through `Sources`, and `cli/common` loads the project over the Factory config for `run`, `check`, `resume`, `finish` and `clean`.
The graph keeps 71 modules and 444 edges, and the mean Ce stays at 6.25.
`cli` stays at its ceiling of 20: `cli/common` catches the `ValueError` of `parse_set` and picks the Library by Run id, so it imports neither `values` nor `library_lookup`.
`config` imports no new module of `src/factory`.

### #260: One `Kind` contract for every registry

Step 2 of #258.
`ports/kind` holds `Kind`, `Choice`, `Problems` and `CheckContext`, and the five registries of `wiring` hold Kinds of it.
Each adapter module defines its own Kind, so `claude_code`, `sbx`, `local_sandbox`, `terminal_workbench`, `github` and `gitlab` each import `kind`, and `adapters` imports 13 modules from outside it, one above its ceiling of 12, which rises to 13.
`config` imports `kind` in place of `sandbox`, because `Project.sandbox` is a `Choice` and `sandbox.env` is `Project.sandbox_env`, so `SandboxChoice` is gone.
`config` holds `Kinds` and hands `pipeline_loader` only the Executor names and the project Executor's `Checks`, so `pipeline_loader` imports neither `kind` nor `sandbox`, and `wiring` no longer imports `pipeline_loader`.
`tracker` makes the `LocalTracker` of a folder with a plain function, so it imports no `kind`.
`wiring` imports `kind` and stays at its ceiling of 20.
The graph goes from 71 modules to 72 and from 444 edges to 449, and the mean Ce from 6.25 to 6.24.
`MAX_EDGES` rises to 449 for the five edges of the adapters past what the step shed, and `MAX_MEAN_CE` drops to 6.24.

### #261: A Port contract per port

Step 3 of #258.
`tests/contracts/` holds one contract module per port, which runs every kind that `wiring` registers, so no module of `src/factory` changes.
The graph keeps 72 modules and 449 edges, and the mean Ce stays at 6.24.

### #262: The PR folder is a kind of `PullRequests`

Step 4 of #258.
`PullRequestFolder` moves from `tracker` to its own module `adapters/pull_request_folder`, which imports only `tracker`, and `wiring` registers it as the PullRequests kind `folder`.
`Tracker.pull_request_folder` returns the folder's path, so `tracker` makes no adapter.
`run_start`, `landing`, `ci_wait` and `run_tools` ask the port for `pushes` and read `absent` CI instead of checking the type, and `ci_fix` hands `ci_guard` the kind's `ci_config_paths`, so no module of `run` changes its imports.
`wiring` imports `pull_request_folder` and takes the Run's frozen Layer from `run_entry.frozen_library` instead of `artifacts`, so it stays at its ceiling of 20.
The graph goes from 72 modules to 73 and from 449 edges to 450, and the mean Ce from 6.24 to 6.16.
`MAX_EDGES` rises to 450, because the new module's two edges are one past the edge the step shed, and `MAX_MEAN_CE` drops to 6.16.

### #263: Ticket and issue ids are opaque text, and `LocalTracker` is an adapter

Step 5 of #258.
`LocalTracker` moves from `tracker` to `adapters/local_tracker`, which takes the place of `pull_request_folder` and holds both Kinds of a Spec folder, so `ports/` holds no adapter class.
`tracker` no longer imports `front_matter`, which `local_tracker` imports instead, and `local_tracker` imports `kind` for its Kinds and `states` for the Ticket states of its footer, which `tracker` keeps for `merged_title` and `not_landed`.
`wiring` imports `local_tracker` in place of `pull_request_folder` and `ports.tracker`'s function, and `cli`, `views` and `run` import no new module: `Tracker.reference` reaches the Run tree as a function.
The graph keeps 73 modules and goes from 450 edges to 452, and the mean Ce from 6.16 to 6.19.
`MAX_EDGES` rises to 452 and `MAX_MEAN_CE` to 6.19, for the edges of `local_tracker` to `kind` and `states`.

### #264: Consumers depend on narrow Tracker roles

Step 6 of #258.
`ports/tracker` declares the roles `SpecSource`, `Closing`, `TicketBoard` and `IssueDesk`, and a Tracker kind makes a `Tracker` that holds the roles it serves, as ADR 0012 decides.
`RunContext` holds the `Closing`, `RunTools` the `TicketBoard` and the `IssueDesk`, and `Projection` takes the `TicketBoard`, all from `tracker`, which they imported already.
`operations` imports `tracker` for the `IssueDesk` it hands `IssueFiling`, and `wiring` asks the Pipeline for its `file_issues` Operations through `Pipeline.operations_of`, so it imports no `pipeline` and stays at its ceiling of 20.
The `run` package keeps its Ce of 30, because `tracker` was outside it already.
The graph keeps 73 modules and goes from 452 edges to 453, and the mean Ce from 6.19 to 6.21.
`MAX_EDGES` rises to 453 and `MAX_MEAN_CE` to 6.21, for the edge from `operations` to `tracker`.

### #265: The Executor writes the Attempt events

Step 7 of #258.
`ports/executor` defines the Attempt events, and `claude_code` writes them next to its raw stream, as ADR 0013 decides.
`Executor` loses `is_progress` and `render_line`, and `Workbench.open_attempt` and the renderer's command line lose the Executor's name.
`render` reads the events from `executor`, which it imported already, and takes from `wiring` only the normalizing of an Attempt from before them, so it no longer imports `config`.
`live` no longer names the `Executor` but imports the events from `executor` still, and `attempt` and `ci_fix` import no new module.
The graph keeps 73 modules and goes from 453 edges to 452, and the mean Ce from 6.21 to 6.19.
`MAX_EDGES` drops to 452 and `MAX_MEAN_CE` to 6.19, for the edge from `render` to `config`.

### #266: One check of a model and effort pair

Step 8 of #258.
`Checks` and the `Executor` port replace `model_problem` and `effort_problem` with `settings_problem(model, effort)`, so an Executor whose efforts differ per model can refuse a pair.
`pipeline_loader` checks the pair it builds after the merge with `defaults`, and no module imports anything new.
The graph keeps 73 modules, 452 edges and a mean Ce of 6.19, and the ceilings stay.

### #267: The factory owns a neutral Agent kit, and each Executor translates it

Step 9 of #258.
`src/factory/kit` holds the skills, the instructions and the MCP servers in no Executor's format, which `ports/executor` reads as an `AgentKit`, and what only Claude Code needs moves to `adapters/claude_code_kit`.
`wiring` takes the kit skills of the `Library` from its `AGENT_KIT` instead of the Claude adapter's `Kit`, and hands the kit to a Run in the `Ports`, which `run_ports` takes from `executor` as it took the `Executor`.
`Executor.assemble` takes the kit, and `Executor.kit_sha` gives way to `translation_version`, which `attempt` hashes with the kit through `fingerprint.kit_sha` from the module it imported for `Variant`.
No module imports anything new, so the graph keeps 73 modules, 452 edges and a mean Ce of 6.19, and the ceilings stay.

### #268: Each Executor declares its quota windows and always reports cost

Step 10 of #258.
`WindowUsage` carries the length of its window, `Executor.reported_windows` and `Checks.reported_windows` name the windows an Executor may report, and `Usage` gets `cost_source` and `thinking_tokens`, so `throttle` drops its table of window lengths.
The `throttle` keys a brake by `<executor>.<window>`, and the bare `five_hour` and `seven_day` stay the default Executor's: `config` resolves them in the project file, and `decision.brake_key` in a `limits` Command and an old `LimitsChanged`, which `inbox` takes from `decision`, so `decision` imports `config` for `DEFAULT_EXECUTOR` and reaches the ceiling of 20.
`wiring` warns of a window its Executor never reports through the `Checks` it holds already, and `stats` and `metrics_memory` read `cost_source` from the payload, so they import nothing new.
The `run` package keeps its Ce of 30, because `config` was outside it already.
The graph keeps 73 modules and goes from 452 edges to 453, and the mean Ce from 6.19 to 6.21.
`MAX_EDGES` rises to 453 and `MAX_MEAN_CE` to 6.21, for the edge from `decision` to `config`.

### #269: A Run mixes Executors, one per Stage

Step 11 of #258.
`Ports.executor` becomes `Ports.executors` by kind name, which `Machine.executors_of` fills with every Executor that `Pipeline.executors` names, and `Attempts`, `RunAgents`, `Isolation` and `RunStart` pick the one of a node from its `AgentSettings`, so the width of the change is the set of modules that took the one Executor, not a new seam.
`pipeline_loader` takes the `Checks` of every Executor and checks each node's pair against its own, `Needs.agent` becomes a tuple that `sbx` creates its Sandbox from, and the Orchestrator brakes each Executor apart in `RunMemory.braked`, whose events `journal` folds by Executor.
`fingerprint` adds `executor` to the fingerprint, so `metrics_memory` and `stats` group on it through the keys they imported already.
No module imports anything new, so the graph keeps 73 modules, 453 edges and a mean Ce of 6.21, and the ceilings stay.

### #270: A dollar budget brake

Step 12 of #258.
A budget goes the way of a brake, from the `throttle` block through `Project`, `RunMemory` and the `limits` Command to the view that shows it, so the width of the change is the set of modules a brake already passes through, not a new seam.
`throttle` holds `split_budgets` and `parse_budget`, `RunMemory.over_budget` answers for `orchestrator` and `ci_wait`, `run_decision` opens and closes `budget_spent`, and `inbox` shares its abandoning between `cancel` and the outcome `finish`.
No module imports anything new, so the graph keeps 73 modules, 453 edges and a mean Ce of 6.21, and the ceilings stay.

### #271: The Run tree display moves behind the Workbench

Step 13 of #258.
The probe, the OSC 7501 records and the words Orca has for each state move from `run_view` to `OrcaWorkbench`, and BEL to `TerminalWorkbench`, behind `Workbench.show_run` and `Workbench.needs_owner`, as ADR 0006 now says.
`run_view` builds a `RunTree` of `ports/workbench` and takes the Workbench from the `Ports`, which adds an edge to `workbench`, so `views` goes from a Ce of 10 to 11 and its ceiling rises with it.
`kind` imports `workbench` for the `OwnerInput` in its `Tools`, because the probe reads the stdin that also holds the owner's answers, and `terminal_workbench` imports `states` for the Orca states.
The graph keeps 73 modules and goes from 453 edges to 456, and the mean Ce from 6.21 to 6.25.
`MAX_EDGES` rises to 456 and `MAX_MEAN_CE` to 6.25, for these three edges.

### #285: Close the gaps the review of the VendorSeams stack found

Step 14 of #258.
A bare `five_hour` or `seven_day` is the window of the project's `executor`: `config` resolves it in the project file, and `inbox` in a `limits` Command and an old `LimitsChanged` with the Executor that `RunMemory` holds, so `decision.brake_key` goes with the edge from `decision` to `config`.
The default kind of each port moves from `config` to `wiring`, which hands them over in `Kinds`, and `Project` takes `workbench` and `executor` without a default.
`config` warns of a brake or a budget on an Executor that runs no node, so `wiring` stays at 20 imports and takes no edge to `throttle`.
`executors: {<kind>: {...}}` gives every Executor of a Run its settings, and `Machine.gh`, `Machine.glab`, `Tools.gh` and `Tools.glab` go, as the GitHub and GitLab adapters reach their CLI on the `PATH`.
The graph keeps 73 modules and goes from 456 edges to 455, and the mean Ce from 6.25 to 6.23.
`MAX_EDGES` falls to 455 and `MAX_MEAN_CE` to 6.23.

### #20: Rich Attempt card in Orca through managed hooks

A new seam, the card of an Attempt, which is why the PR touches more than five modules.
The Executor writes the card facts as `card` Attempt events of `ports/executor`, `render` hands them to the `Card` it declares for itself, `wiring` picks the card that `--card` names, and `adapters/orca_card` maps them to Orca's hook events.
`wiring` stays at 20 imports, because it reaches the card through `terminal_workbench`, whose `OrcaWorkbench` names it.
The graph goes from 73 modules to 74 and from 455 edges to 456, and the mean Ce from 6.23 to 6.16.
`MAX_EDGES` rises to 456 for the edge from `terminal_workbench` to `orca_card`, and `MAX_MEAN_CE` falls to 6.16.

### #305: The Run graph format, its check, and the default Run graph

Step 1 of #304, which is why the PR touches more than five modules.
A Run graph is one more use of the loader, the check and the Library that a Pipeline goes through, so `pipeline`, `pipeline_checks`, `pipeline_loader`, `library_lookup` and `config` each grow in place, and `orchestrator`, `landing`, `ci_wait`, `run_start`, `run_state`, `run_view`, `wiring`, `events` and `states` take the small part each owns.
`cli` gets the command `library` inside its one module.
No module and no import edge is added, because each of these already imported what its part needs, so the graph keeps 74 modules and 456 edges and the mean Ce stays 6.16.
`MAX_EDGES` and `MAX_MEAN_CE` stay.

### #306: The Describer and the PR are Run nodes

Step 2 of #304, which is why the PR touches more than five modules.
`land` splits into the Run Stage `describe` and the node `open_pr`, so `pipeline`, `pipeline_checks`, `pipeline_loader` and `config` read and check them, `landing` and `orchestrator` run them, `run_state`, `metrics_memory`, `metrics` and `stats` keep what they tell, and `run_agent` only changes its words.
`landing` trades its import of `fingerprint` for one of `metrics_memory`, because its events now tell `factory stats` their cost, so the graph keeps 74 modules and 456 edges and the mean Ce stays 6.16.

### #308: Tracker Operations `assign`, `label` and `comment`, at both levels

Step 4 of #304, which is why the PR touches more than five modules, and it brings the `TicketBoard.assign` of #300 with it, without the assignment at the start of every Run.
`pipeline` holds the `TrackerAction` of both levels and `pipeline_loader` reads it, `operations` carries it out in `TrackerOperations`, `orchestrator` runs it as a Run node and `wiring` refuses it on a Tracker without a `TicketBoard`.
`assign` and `spec_id` join `ports/tracker` and each Tracker kind, `tracker_cli` only rewords a comment, `run_view` and `run_context` get `warn`, and `events`, `run_state` and `moves` take the small part each owns.
`TrackerOperations` is a class of `operations` and no module of its own, because it needs only what `operations` imported already, and a module would add seven edges past the ceiling this Spec holds.
No module and no import edge is added, so the graph keeps 74 modules and 456 edges and the mean Ce stays 6.16.
`MAX_EDGES` and `MAX_MEAN_CE` stay.

### #309: Selection is a Run node, and `filter` takes Tickets by label

Step 5 of #304, which is why the PR touches more than five modules, and it reuses the label reading of #301 for its `filter`, without the project-wide `tracker.ready_label`.
`pipeline` holds the kinds `all` and `filter`, `pipeline_loader` reads the `with` of `filter`, `pipeline_checks` holds the order, and `orchestrator` carries the node out, with `artifacts` writing its Artifact and `journal` dropping the rows of what it left out.
`ports/tracker` and each Tracker kind report labels and a blocker they did not read in place of refusing it, `cli` refuses that blocker for a graph without a `filter`, `run_context` narrows the snapshot to the Tickets of the Run, and `events`, `run_state`, `run_start` and `run_view` take the small part each owns.
The node is no module of its own, because `orchestrator` needs only `SpecSnapshot.carrying` and `RunDir.write_selection`, which it reaches through the context it holds.
No module and no import edge is added, so the graph keeps 74 modules and 456 edges and the mean Ce stays 6.16.
`MAX_EDGES` and `MAX_MEAN_CE` stay.

### #310: An agent selects the Tickets and their order

Step 6 of #304, which is why the PR touches more than five modules.
`pipeline`, `pipeline_checks`, `pipeline_loader`, `library_lookup` and `config` read and check a Run Stage whose Role `selects`, `run_agent` and `events` name the Planner, `run_state` keeps what its events tell, `ports/tracker` holds the rule of a plan next to `carrying`, and `landing`, `run_agents` and `orchestrator` run it.
`Planner` is a class of `landing` and no module of its own, as `TrackerOperations` is of `operations`: it needs only what `landing` imported already for the Describer, and a module would add edges past the ceiling this Spec holds, and `orchestrator`, at 20 imports, could not import it.
No module and no import edge is added, so the graph keeps 74 modules and 456 edges and the mean Ce stays 6.16.
`MAX_EDGES` and `MAX_MEAN_CE` stay.

### #311: A Stage reads comments from the Tracker as an input

Step 7 of #304, which is why the PR touches more than five modules.
`Discussions` joins `ports/tracker`, `github`, `gitlab` and `tracker_cli` read it and sign the factory's comments, `pipeline` and `pipeline_loader` hold the Tracker input, `pipeline_checks` and `wiring` refuse it where it has nothing to read, `operations`, `orchestrator`, `run_tools` and `run_entry` read it before the Attempt takes its slot, and `attempt`, `inputs`, `live` and `run_state` carry it through the Brief to the `handled` of the Journal.
`TrackerInputs` is a class of `operations` and no module of its own, as `TrackerOperations` is, because `operations` imports the Tracker's port already, so `attempt` and `inputs` never do.
No module and no import edge is added, so the graph keeps 74 modules and 456 edges and the mean Ce stays 6.16.
`MAX_EDGES` and `MAX_MEAN_CE` stay.

### #312: A Stage posts its findings to the Tracker, and a review loop runs through it

Step 8 of #304, and the PR touches five modules of `src/factory`.
`pipeline` and `pipeline_loader` read the `from`, `field` and `each` of a `comment` and the `posted_by` of a Tracker input, `pipeline_checks` holds them against the Role of the Stage and refuses what posts nothing to read, `operations` posts from the output and reads the posts back, and `deciders` holds the function `findings` that ends the loop.
The rest of the PR is the Library's new Pipeline, its two Roles and its schema, so no adapter and no port changes: the mark of a post goes in the comment's text, as the factory's signature does, and reads the same on every Tracker.
No module and no import edge is added, so the graph keeps 74 modules and 456 edges and the mean Ce stays 6.16.
`MAX_EDGES` and `MAX_MEAN_CE` stay.

### #307: The CI wait and the CI fixer are Run nodes

Step 3 of #304, which is why the PR touches more than five modules.
`pipeline`, `pipeline_checks`, `pipeline_loader` and `config` read and check the Run Stage `ci_fix`, Run Gates and Decision nodes, `run_agent` loses the fields of the block `ci_fix`, `orchestrator` walks them, `ci_wait` and `ci_fix` split at the graph's route on `red`, `run_decision`, `decision` and `run_state` take a Decision's outcomes from its event, `run_agents` clears what a killed Round left, `run_view` reads an answer from any such Decision and `events` gets `CiFixCommitted`.
`ci_wait` no longer imports `ci_fix`, and `ci_fix` no longer imports `question` or `sandbox`, and `ci_wait` imports `pipeline` for the Decision it asks, so the graph keeps 74 modules and goes to 454 edges and a mean Ce of 6.14.
`MAX_EDGES` and `MAX_MEAN_CE` go down to those.

### #313: A Run waits for an event on the Tracker

Step 9 of #304, which is why the PR touches more than five modules.
`pipeline`, `pipeline_checks`, `pipeline_loader` and `config` read and check the node `wait_tracker` and `tracker_poll`, `values` takes the unit `d` of `timeout: 3d`, `ports/tracker` holds the one member `events` that `PullRequests` and `Discussions` share, `github`, `gitlab` and `local_tracker` fill it, `wiring` refuses a wait on the Spec where it has nothing to read, `orchestrator` walks the node and `events` and `run_state` keep what it tells.
`TrackerWaiter` is a class of `ci_wait` and no module of its own, because it needs only what `ci_wait` imported already for the wait beside it, and `orchestrator`, at 20 imports, could not import a new module.
No module and no import edge is added, so the graph keeps 74 modules and 454 edges and the mean Ce stays 6.14.
`MAX_EDGES` and `MAX_MEAN_CE` stay.

### #314: A Run merges its own PR

Step 10 of #304, which is why the PR touches more than five modules.
`pipeline`, `pipeline_checks` and `pipeline_loader` read and check the node `merge_pr` and the Addresser, `run_agent` holds the Addresser beside the CI fixer, `ports/tracker` holds the members `merged` and `merge` of `PullRequests`, `github`, `gitlab` and `local_tracker` fill them, `landing` carries the merge out because it holds all that it needs, `ci_fix` works the Rounds of the Addresser and `operations` reads its comments, which `orchestrator` hands it so `ci_fix` gains no import, `ci_wait`, `events` and `run_state` keep what they tell, and `cli/common` reports it.
No module and no import edge is added, so the graph keeps 74 modules and 454 edges and the mean Ce stays 6.14.
`MAX_EDGES` and `MAX_MEAN_CE` stay.

### #316: A skill helps the owner write a Pipeline or a Run graph

Step 11 of #304.
`factory diagram` and `factory skill install` are modules of the `cli` package, which counts as one module, and they read the Library through `library_lookup`, and the place of the owner's skills through `wiring`, which takes it from `claude_code`.
The diagram adds the edge from `cli` to `pipeline` for the closed sets of node kinds and outcomes, and `took` in `cli/common` sheds the edge from `cli` to `clock` by reading the clock of the Machine, so `cli` stays at 20 imports.
The graph keeps 74 modules and 454 edges and the mean Ce stays 6.14.
`MAX_EDGES` and `MAX_MEAN_CE` stay.

### #317: `factory doctor`, `factory init` and a skill set the factory up

Step 12 of #304.
`factory doctor`, `factory init` and the table of stacks are modules of the `cli` package, which counts as one module.
A Kind gets a `diagnose`, which `factory doctor` asks through `wiring`, so `cli` names no vendor and the ports gain no edge to `kind`.
`try_gates` runs the Gates outside a Run and lives in `probes`, whose imports it needs; `cli` reaches it through `run_entry`, whose edge to `probes` is new.
`drive` in `cli/common` sheds the edge from `cli` to `sleep_block`, so the count holds and `cli` drops to 19 imports.
The graph keeps 74 modules and 454 edges and the mean Ce stays 6.14.
`MAX_EDGES` and `MAX_MEAN_CE` stay.

## How to re-measure

```
make measure-coupling
```

Compare `modules per commit` and the Jaccard table against this document after each step, and update the numbers here when a step lands.
