# Coupling and cohesion of `src/factory`

A baseline of how tightly the modules of `src/factory` hang together, taken before the refactor of the package structure.
`make measure-coupling` prints the numbers from the imports and the git history, and `tools/measure/coupling.py` explains each column.
Numbers are from `main` at `287ba10`, with PRs #225 and #226 still open.

## Why this document exists

Two small, self-contained features each touched a large share of the code:

| PR | Feature | Files | Of them `src/factory` | New modules |
| --- | --- | --- | --- | --- |
| #225 | `effort` next to `model` on Pipeline nodes, passed as `--effort` | 23 | 10 | 0 |
| #226 | Run-level Stage `ci_fix`, a fixer for red CI on the final PR | 34 | 16 | 2 |

A field on a Pipeline node touched 10 of 55 modules.
A new Run-level agent touched 14 existing modules besides its own two.
The sections below say where that spread comes from, which of it the module graph shows and which of it it hides.

## What the import graph says

| Measure | Value |
| --- | --- |
| Modules | 56, `cli` counted as one |
| Import edges | 307 |
| Density | 0.10 |
| Mean fan-out (Ce) | 5.5 |
| Cycles | 0, `tests/test_layers.py` forbids them |
| Modules with Ce ≥ 10 | 12 |
| Modules with Ca ≥ 10 | 11 |

At the level of modules the coupling is moderate.
One module in ten pairs is joined, there are no cycles, and the layer rules in `docs/architecture/dependencies.md` hold.
The graph is not what makes a small feature wide.

The hubs are few and known:

| Module | Ca | Ce | Names out | Lines | Role in the graph |
| --- | --- | --- | --- | --- | --- |
| `orchestrator` | 2 | 30 | 63 | 658 | imports more than half of the package |
| `cli` | 0 | 25 | 57 | 668 | the second composition root beside `wiring` |
| `attempt` | 10 | 17 | 41 | 530 | both a phase and the `Ports` bundle every phase takes |
| `config` | 10 | 10 | 29 | 749 | project loader, Pipeline loader and validation in one |
| `run_start` | 2 | 19 | 25 | 226 | a phase with 4 unrelated groups of definitions |
| `events` | 21 | 0 | 21 | 81 | one enum every writer and every reader of the Journal imports |
| `states` | 19 | 0 | 42 | 28 | 10 constants with nothing joining them |
| `pipeline` | 16 | 0 | 58 | 244 | the domain model, 58 names taken by 16 modules |
| `tracker` | 14 | 2 | 45 | 285 | Tracker, PullRequests and CI status in one port |

The widest edges are `config -> pipeline` with 15 names and `pipeline_checks -> pipeline` with 12.
The three modules are one concern, the Pipeline model and its loading and checking, split by layer into three files.

## What the git history says

Over the last 123 commits that touch `src/factory`:

| Measure | Value |
| --- | --- |
| Modules per commit, median | 3 |
| Modules per commit, p90 | 9 |
| Commits that touch 8 or more modules | 21 of 123 |
| Commits that touch `orchestrator` | 70 |
| Commits that touch `cli` | 50 |
| Commits that touch `config` | 41 |

The pairs that change together most, with the share of their commits they share (Jaccard):

| Pair | Together | Jaccard |
| --- | --- | --- |
| `cli` + `orchestrator` | 35 | 0.41 |
| `config` + `orchestrator` | 32 | 0.41 |
| `cli` + `journal` | 22 | 0.43 |
| `github` + `tracker` | 11 | 0.58 |
| `journal` + `tracker` | 11 | 0.39 |
| `config` + `git` | 18 | 0.38 |
| `config` + `pipeline` | 16 | 0.36 |

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
| `SETTINGS_FIELDS`, `DESCRIBER_FIELDS`, `DECIDER_FIELDS`, `ESCALATE_FIELDS` | `config` |
| `AgentWork`, `Stage`, `Decider`, `Describer`, `Escalate` | `pipeline` |
| `AttemptRequest` | `executor` |
| the fingerprint dict | `fingerprint` |
| `FINGERPRINT` | `metrics` |
| `GROUPS` | `stats` |
| `ModelNames` | `config`, built in `wiring` |

PR #225 adds `effort` to each of them, and that is its 10 modules.
Only two of the ten hold behaviour that is about `effort`: the `--effort` flag in `claude_code` and the check of the value in `config`.
The other eight carry a field they do not read.
There is no value type for "the settings a Fresh agent runs with" that travels whole from the Pipeline file to the Executor and into the Journal.

### 2. Every feature registers in the same central tables

A feature that adds an event writes the name into `Event` and then teaches each reader of the Journal about it:

| Reader | Distinct events it matches |
| --- | --- |
| `run_state.apply` | 33 |
| `journal` | 27 |
| `metrics` | 21 |
| `orchestrator` | 19 |
| `attempt` | 13 |
| `timeline` | 9 |
| `operations` | 9 |
| `run_start` | 8 |
| `run_view` | 8 |

The same holds for `artifacts`, which has one method per directory layout, for the field tuples of `config`, and for the slot names of `journal`.
PR #226 adds 8 events, and the events then take it into `run_state`, `run_view`, `timeline`, `journal` and `artifacts`.
Each table is cohesive by mechanism, every one of them replays events, and not by feature, so every feature cuts across all of them.

### 3. A Run-level agent is not a concept, it is a copy

The Describer is an agent that no Ticket owns.
It has its own slot, its own five events (`DescriberStarted`, `DescriberFailed`, `DescriberSandboxRemoved`, `DescriberSandboxLeftBehind`, `DescriberCleanupFailed`), its own branch in `attempt`, `landing`, `pipeline_checks`, `config`, `run_view`, `fingerprint` and `artifacts`.
12 modules name it.
The CI fixer of PR #226 is the second such agent, and it copies the pattern: `CI_FIX_SLOT`, six `CiFix*` events, a `CiFix` model next to `Describer`, a `_ci_fix` loader next to `_describer`, a `ci_fix` phase in `run_view` next to `describe`.
The PR generalises one piece, `Attempts.work_apart`, and copies the rest.
A third Run-level agent will cost the same 14 modules again.

### 4. The hubs grow with every feature

- `orchestrator` builds every phase and passes each its tools one by one.
  PR #226 touches it only to pass three more constructor arguments to `CiWaiter`.
- `cli` is a second composition root.
  It builds `Ports`, resolves the base, freezes the Library and builds `RunContext`, so most engine features reach it.
- `config` loads the project file, loads the Pipeline file and validates both, 749 lines and 56 definitions.
  Every field of the Pipeline schema is a change to it.

## Cohesion

The column `components` of the report counts the groups of top-level definitions that name each other inside a module.
Most modules are one group, because most modules are one class with its helpers.
The low-cohesion modules are the small bags of constants and the phase modules that gather unrelated helpers:

| Module | Definitions | Components | Singletons |
| --- | --- | --- | --- |
| `states` | 10 | 10 | 10 |
| `names` | 4 | 4 | 4 |
| `run_start` | 5 | 4 | 3 |
| `question` | 8 | 4 | 3 |
| `tracker_cli` | 7 | 4 | 2 |
| `pipeline` | 22 | 3 | 2 |
| `deciders` | 15 | 3 | 1 |

The bigger cohesion problem is the one the import graph cannot count.
`run_state`, `metrics`, `timeline` and `run_view` are each one connected component, and each knows every feature of the engine.
`attempt` is one component too, and it holds both the charge of an Attempt and the `Ports` bundle that nine other modules import for reasons that have nothing to do with Attempts.

## Goals of the refactor

- A feature of the size of #225 or #226 lands in the modules that own its behaviour and in no other.
  The guard is the measure above: p90 of modules per commit from 9 to 5 or fewer, and no pair outside port and adapter with Jaccard above 0.4.
- The package tree says what the layers table in `dependencies.md` says today, so a reader sees the structure in `ls` and the layer test guards the tree, not a hand-kept list.
- Each module has one reason to change (SRP), and a new kind of node, event or Run-level agent is an addition and not an edit of every reader (OCP).
- Data travels as value types, not as fields copied by hand.

## Direction

The order matters: the value types and the Run-level agent first, because they shrink the hubs, and the package tree last, because moving files before the seams are right moves the problem with them.

1. **An `AgentSettings` value type.**
   One frozen dataclass for `model`, `effort`, `profile`, `attempts`, `timeout`, `stall` and `executor`.
   `Stage`, `Decider`, `Describer` and `CiFix` hold one, `AttemptRequest` takes one, the fingerprint and `metrics` read it.
   The field tuples of `config` come from `dataclasses.fields`, not from a hand-written list.
   A new setting is then one field, its parse and its use in the adapter.
2. **A Run-level agent as one module.**
   What the Describer and the CI fixer share, a slot, the started/finished/cleanup events, the Artifacts directory, the fingerprint, the `run_view` phase, becomes one `run_agent` with a name parameter.
   The Describer and the fixer become two uses of it.
3. **Feature-owned Journal handling.**
   The readers of the Journal keep one central dispatch each, but what an event does to `RunMemory`, to the timeline and to the metrics lives next to the event, in the module that writes it.
   The dispatch tables become registries the modules fill, so adding an event adds no line to `run_state`, `timeline` or `metrics`.
   This is the one step with a design choice in it: typed event classes with methods, or plain registration of handlers per event name.
   Either removes the five-module ripple; the second keeps the Journal's rows as plain JSON, which `docs/adr/` relies on.
4. **Split the hubs along the lines `dependencies.md` already names.**
   `Ports` out of `attempt` into `run_context`.
   The Commands of the inbox out of `orchestrator` into a phase module.
   `_PipelineLoader` out of `config` into its own module next to `pipeline` and `pipeline_checks`.
   The start of a Run out of `cli` into one engine entry.
5. **Packages by concept.**
   With the seams in place the flat 55 modules fold into packages that match the groups of the layer test:
   `pipeline/` (model, loader, checks, library), `run/` (orchestrator, phases, state, memory), `journal/` (events, storage, custody, commands), `ports/`, `adapters/`, `views/` (run_view, timeline, metrics, stats), `cli/`.
   `tests/test_layers.py` then reads the rules from the tree, and a module in the wrong package fails on import direction alone.

## How to re-measure

```
make measure-coupling
```

Compare `modules per commit` and the Jaccard table against this document after each step, and update the numbers here when a step lands.
