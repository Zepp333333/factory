# Abstractions

## Selecting an adapter

`wiring` is the composition root and the only module that imports an adapter.
It holds `Machine` and one registry per port, and each registry maps a kind name to a factory of the adapter:

| Registry | Kinds | Factory |
| --- | --- | --- |
| `EXECUTORS` | `claude-code` | `() -> Executor` |
| `SANDBOXES` | `local`, `sbx` | `(SandboxSettings) -> Sandbox` |
| `WORKBENCHES` | `terminal`, `orca` | `() -> Workbench` |
| `TRACKERS` | `github`, `gitlab`, `local` | a `TrackerKind` |

Adding a kind to a port takes one `register` call in `wiring`.
`tests/test_wiring.py` registers a fake kind of every port and runs it through `factory check` and `factory run`.

- The project file names the kinds: `executor`, `sandbox.kind` and `workbench`.
  `wiring.kinds()` lists the registries' names, and `config.load_project` takes them as `Kinds` and rejects any other name, so `config` imports no adapter.
  A Stage, Describer or Decider may set `executor` too, and it is validated the same way.
- `executor` defaults to `claude-code`, so a project file with `executor: claude-code` and one without it run the same.
- A `TrackerKind` is picked by the Spec link: each kind that has an `owns` rule (`github` by its host, `gitlab` by its `/-/issues/` path) claims its links, and the kind without one (`local`) takes the rest.
  A kind may also bring a `preflight` and `warnings` for its CLI.
- `Machine` still replaces the pick as a whole in tests: `Machine.executor`, `Machine.sandbox_for` and `Machine.workbench` win over the registries when set.
