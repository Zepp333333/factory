# Dependencies between modules

`src/factory` is layered.
`tests/test_layers.py` parses the imports of every module, lazy imports and `TYPE_CHECKING` imports included, and fails on any import cycle and on any import that breaks a rule below.
A module that is in no group fails it too, so a new module picks its group in the test.

## Groups

| Group | Modules | May import |
| --- | --- | --- |
| foundation | `clock`, `events`, `front_matter`, `git`, `names`, `render`, `spawn`, `states`, `values` | foundation |
| domain | `deciders`, `fingerprint`, `gates`, `library_lookup`, `pipeline`, `pipeline_checks`, `throttle` | foundation |
| ports | `executor`, `sandbox`, `tracker`, `workbench` | foundation, domain |
| adapters | `claude_code`, `github`, `gitlab`, `sbx`, `tracker_cli` | foundation, domain, ports |
| project | `config` | foundation, domain, ports |
| storage | `commands`, `custody`, `journal` | foundation |
| state | `artifacts`, `metrics`, `projection`, `question`, `recovery`, `run_state`, `run_view`, `stats`, `timeline` | foundation, domain, ports, storage |
| engine | `attempt`, `clean`, `decision`, `merge_window`, `orchestrator`, `sleep_block` | foundation, domain, ports, project, storage, state |
| wiring | `wiring` | foundation, domain, ports, adapters, project, storage, state, engine |
| root | `cli` | every group but adapters |

A group may always import its own modules.
Only `wiring`, the composition root, imports an adapter, and the adapter classes that live in `sandbox` and `workbench`.
`wiring` is the top of the graph, and `cli` takes `Machine` from it.

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

## What the layers keep apart

- The domain model (`pipeline`, `throttle`, `fingerprint`, `library_lookup`) imports no port.
  The ports import it: `executor` uses `Profile` from `pipeline` and `WindowUsage` from `throttle`.
- `Read` lives in `pipeline`, and `library_lookup` imports `pipeline`, so the two no longer import each other.
- `fingerprint` takes the kit hash as a function, not the `Executor`.
- `TERMINAL_NODES` lives in `states`.
- `journal` stores the payload of a brake, and `run_view` words it, so `journal` imports neither `pipeline` nor `throttle`.
- `names` holds `integration_branch`, `ticket_branch`, `abandoned_ref` and `sandbox_name`, so `clean` imports neither `orchestrator` nor `attempt`.

## Known exceptions

None. A rule that needs an exception is a rule to change here and in the test together.
