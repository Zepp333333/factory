# Dependencies between modules

`src/factory` is layered.
`tests/test_layers.py` parses the imports of every module, lazy imports and `TYPE_CHECKING` imports included, and fails on any import cycle and on any import that breaks a rule below.
A module that is in no group fails it too, so a new module picks its group in the test.

## Groups

| Group | Modules | May import |
| --- | --- | --- |
| foundation | `clock`, `events`, `front_matter`, `git`, `names`, `spawn`, `states`, `values` | foundation |
| domain | `deciders`, `fingerprint`, `gates`, `library_lookup`, `pipeline`, `pipeline_checks`, `throttle` | foundation |
| ports | `executor`, `sandbox`, `tracker`, `workbench` | foundation, domain |
| adapters | `claude_code`, `github`, `gitlab`, `sbx`, `tracker_cli` | foundation, domain, ports |
| project | `config` | foundation, domain, ports |
| storage | `commands`, `custody`, `journal` | foundation |
| state | `artifacts`, `metrics`, `projection`, `question`, `recovery`, `run_state`, `run_view`, `stats`, `timeline` | foundation, domain, ports, storage |
| engine | `attempt`, `ci_wait`, `clean`, `decision`, `landing`, `live`, `merge_window`, `operations`, `orchestrator`, `probes`, `run_context`, `run_decision`, `run_start`, `sleep_block` | foundation, domain, ports, project, storage, state |
| wiring | `render`, `wiring` | foundation, domain, ports, adapters, project, storage, state, engine |
| root | `cli` | every group but adapters |

A group may always import its own modules.
Only `wiring`, the composition root, imports an adapter, and the adapter classes that live in `sandbox` and `workbench`.
`render`, the entry point of the renderer process, takes the Executor from the registry of `wiring`.
`wiring` is the top of the graph, and `cli` takes `Machine` from it.
`cli` is a package, and the test counts it as one module of the graph.
It holds `main` and the parser in `__init__`, one module per command (`check`, `run`, `status`, `stats`, `decide`,
`cancel`, `override`, `limits`, `resume`, `finish`, `clean`, `attach`) and `common` for what several commands share:
the host tool errors, the Tracker of a Spec, the ports of a Run, an orphaned Run taken from the Journal
and driving a Run to its end.
No module of the package imports more than half of `src/factory`.

## Graph

```mermaid
flowchart TD
    root --> wiring
    root --> engine
    wiring --> engine
    wiring --> adapters
    wiring --> project
    engine --> state
    engine --> project
    state --> storage
    state --> ports
    project --> ports
    adapters --> ports
    ports --> domain
    domain --> foundation
    storage --> foundation
```

Every group also imports the foundation, and `wiring`, `root`, `engine`, `state` and `project` import the domain.
The arrows show the main direction only.

## The Orchestrator and its phases

`orchestrator` keeps the loop, the scheduling of Tickets, the Commands in the inbox and the dispatch to the modules of the engine.
Each phase of a Run lives in a module of its own, as `attempt` and `decision` do:

- `run_start`: base resolution, the Integration branch, the Library freeze, resuming an orphaned Run and reconciliation.
- `landing`: the PR text with the Describer, the push of the Integration branch and the PR through `PullRequests`.
- `ci_wait`: the wait for the CI of the PR and the Decision `ci_failed`.
- `run_decision`: the Decision `infra_failure` and the owner's answers to the Decisions of the Run.
- `probes`: the Probes of Gates and their verdicts.
- `operations`: `sync` and `merge`, and the clean-up of merged and abandoned Tickets.

None of them imports `orchestrator`, and each takes the Run's `RunContext` from `run_context`.
A `RunContext` is frozen: the Run's id, directory, Integration branch, Spec snapshot, Tracker, base commit and `RunMemory`.
`cli` builds it before the Orchestrator, with `RunContext.new` for a new Run and `RunContext.replayed` from the events of a Run that resumes or finishes.
No phase writes a field of the Orchestrator.
What a phase learns goes into an event, and `RunMemory` keeps it: the PR, the pushed commit, how the CI wait ended and an owner's `abort`.
`Lander.land` returns the commands that finish a failed landing by hand.
`landing` is imported by `run_start` for the remote a Run lands through, and `ci_wait` by `run_decision` for the outcomes of `ci_failed`.

## What the layers keep apart

- The domain model (`pipeline`, `throttle`, `fingerprint`, `library_lookup`) imports no port.
  The ports import it: `executor` uses `Profile` from `pipeline` and `WindowUsage` from `throttle`.
- `Read` lives in `pipeline`, and `library_lookup` imports `pipeline`, so the two no longer import each other.
- `fingerprint` takes the kit hash as a function, not the `Executor`.
- `TERMINAL_NODES` lives in `states`.
- `journal` stores the payload of a brake, and `run_view` words it, so `journal` imports neither `pipeline` nor `throttle`.
- `run_state` keeps only the state the Journal replays, so it imports neither `executor`, `sandbox` nor `workbench`.
  `live` holds what the Orchestrator's process adds: `LiveTicket` with the running agent and Probe, the handles
  (`AttemptHandle`, `Process`, `AttemptTerminal`) and `Silence`.
- The Executor tells progress (`is_progress`) and renders its stream (`render_line`), so only `claude_code` reads a Claude event.
  `timeline` reads the turns and cost of an Attempt from the `usage` of `AttemptFinished`, not from the stream.
  `Silence` asks the Executor, and `render`, which runs as one process per Attempt, renders through the Executor
  named on its command line, so `workbench` passes the name of the Attempt's Executor on.
- `Failure` lives in `states`, so `run_state` needs no `executor` for it, and `marker` lives in `workbench`.
- `names` holds `integration_branch`, `ticket_branch`, `abandoned_ref` and `sandbox_name`, so `clean` imports neither `orchestrator` nor `attempt`.

## Known exceptions

None. A rule that needs an exception is a rule to change here and in the test together.
