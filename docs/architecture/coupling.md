# Coupling and cohesion of `src/factory`

A baseline of how tightly the modules of `src/factory` hang together, taken before the refactor of the package structure.
`make measure-coupling` prints the numbers from the imports and the git history, and `tools/measure/coupling.py` explains each column.
Numbers are from `main` at `b42ea43`, after PRs #225, #226 and #227 landed.
The import graph is re-measured after step 4 of the plan, and the git history counts the commits up to `af7aee1`, where step 4 starts.

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
| Modules | 62, `cli` counted as one |
| Import edges | 380 |
| Density | 0.10 |
| Mean fan-out (Ce) | 6.1 |
| Cycles | 0, `tests/test_layers.py` forbids them |
| Modules with Ce ≥ 10 | 15 |
| Modules with Ca ≥ 10 | 15 |

At the level of modules the coupling is moderate.
One module in ten pairs is joined, there are no cycles, and the layer rules in `docs/architecture/dependencies.md` hold.
The graph is not what makes a small feature wide.

The hubs are few and known:

| Module | Ca | Ce | Names out | Lines | Role in the graph |
| --- | --- | --- | --- | --- | --- |
| `orchestrator` | 2 | 34 | 73 | 789 | imports more than half of the package |
| `cli` | 0 | 24 | 57 | 667 | the second composition root beside `wiring` |
| `attempt` | 3 | 20 | 51 | 482 | the charge of an Attempt, imported only by the modules that run one |
| `config` | 13 | 11 | 32 | 794 | project loader, Pipeline loader and validation in one |
| `run_start` | 2 | 21 | 32 | 244 | a phase with 4 unrelated groups of definitions |
| `events` | 25 | 0 | 67 | 145 | the enum and the registries of handlers, which every writer and reader of the Journal imports |
| `states` | 20 | 0 | 40 | 28 | 10 constants with nothing joining them |
| `pipeline` | 20 | 1 | 66 | 258 | the domain model, 66 names taken by 20 modules |
| `tracker` | 15 | 2 | 49 | 293 | Tracker, PullRequests and CI status in one port |

The widest edges are `config -> pipeline` with 16 names and `pipeline_checks -> pipeline` with 12.
The three modules are one concern, the Pipeline model and its loading and checking, split by layer into three files.

## What the git history says

Over the last 133 commits that touch `src/factory`:

| Measure | Value |
| --- | --- |
| Modules per commit, median | 3 |
| Modules per commit, p90 | 10 |
| Commits that touch 8 or more modules | 26 of 133 |
| Commits that touch `orchestrator` | 73 |
| Commits that touch `cli` | 51 |
| Commits that touch `config` | 47 |

The pairs that change together most, with the share of their commits they share (Jaccard):

| Pair | Together | Jaccard |
| --- | --- | --- |
| `cli` + `journal` | 22 | 0.41 |
| `cli` + `orchestrator` | 36 | 0.41 |
| `journal` + `tracker` | 12 | 0.39 |
| `config` + `orchestrator` | 34 | 0.40 |
| `config` + `pipeline` | 21 | 0.42 |
| `github` + `tracker` | 12 | 0.57 |
| `config` + `git` | 19 | 0.37 |

`github` + `tracker` is the port and its adapter, and that pair is expected.
The rest are modules that the import graph keeps apart but that a feature cannot change one without the other.
`orchestrator` is in more than half of all commits.

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
`attempt` is one component too, and since step 1 it holds only the charge of an Attempt: `Ports` lives in `run_context`, and the three modules that import `attempt` run Attempts.

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

### Step 6: packages by concept

| Field | Value |
| --- | --- |
| Scope | the flat package folds into packages that match the groups of the layer test |
| Touches | every module, by `git mv` and import rewrite only |
| Done when | `ls src/factory` shows the layers, and `tests/test_layers.py` derives the group of a module from its package |

| Package | Modules of today |
| --- | --- |
| `factory/core/` | `clock`, `events`, `front_matter`, `git`, `names`, `spawn`, `states`, `values` |
| `factory/pipeline/` | `pipeline`, `pipeline_loader`, `pipeline_checks`, `gates`, `deciders`, `fingerprint`, `library_lookup`, `throttle` |
| `factory/ports/` | `executor`, `sandbox`, `tracker`, `workbench` |
| `factory/adapters/` | `claude_code`, `github`, `gitlab`, `sbx`, `tracker_cli`, `LocalSandbox`, `TerminalWorkbench`, `OrcaWorkbench` |
| `factory/journal/` | `journal`, `commands`, `custody`, `run_state`, `recovery`, `question`, `projection` |
| `factory/run/` | `orchestrator`, `run_context`, `run_entry`, `run_start`, `attempt`, `run_agent`, `landing`, `ci_wait`, `ci_fix`, `inbox`, `run_decision`, `decision`, `probes`, `operations`, `issue_filing`, `merge_window`, `live`, `clean`, `sleep_block` |
| `factory/views/` | `run_view`, `timeline`, `metrics`, `stats`, `artifacts` |
| `factory/` | `config`, `wiring`, `render`, `cli/` |

One PR per package, each a pure move, so a review reads the import rewrites and nothing else.
`LocalSandbox`, `TerminalWorkbench` and `OrcaWorkbench` move out of the ports into `adapters`, which closes the known exception of `dependencies.md` and lets the layer test drop `ADAPTER_CLASSES`.
`tests/test_layers.py` then keeps only the table of which package may import which, and reads the membership from the tree.

### Step 7: guard

| Field | Value |
| --- | --- |
| Scope | the measure becomes a check |
| Touches | `tests/test_layers.py`, `tools/measure/coupling.py` |
| Done when | CI fails on a module above the fan-out ceiling, and the co-change numbers are in the review of every PR that touches more than five modules |

- `tests/test_layers.py` takes a ceiling of Ce per module (20) and per package, so a hub cannot grow back.
- A PR that touches more than five modules of `src/factory` says in its description which step of this document explains the width, or what new seam it found.

### What the plan leaves alone

- The Journal's rows, the Artifacts layout and the CLI output, because the tests assert on them and the ADRs name them.
- The port protocols `Executor`, `Sandbox`, `Workbench`, `Tracker` and `PullRequests`, besides the move of `ModelNames` to the Executor.
- `tracker` as one port for Tracker, PullRequests and CI status.
  Splitting it is a separate decision with its own ADR, because two adapters would gain a third class each.

### Expected numbers after the plan

| Measure | Now | Target |
| --- | --- | --- |
| Modules per commit, p90 | 10 | ≤ 5 |
| Pairs with Jaccard > 0.4 outside port and adapter | 3 | 0 |
| `orchestrator` Ce | 34 | ≤ 20 |
| `attempt` Ca | 3 | ≤ 4 |
| `config` lines | 794 | < 400 |
| Modules a new Pipeline setting touches | 3 | 3 |
| Modules a new Run-level agent touches | 3 plus its own: `run_agent`, `events` and the phase that starts it | 1 plus its own |
| Modules a new event touches | 1 plus its writer: `events` | 1 plus its writer |

## How to re-measure

```
make measure-coupling
```

Compare `modules per commit` and the Jaccard table against this document after each step, and update the numbers here when a step lands.
