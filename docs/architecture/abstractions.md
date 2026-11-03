# Abstractions

## Selecting an adapter

`wiring` is the composition root and the only module that imports an adapter.
It holds `Machine` and one registry per port, and each registry maps a kind name to a factory of the adapter:

| Registry | Kinds | Factory |
| --- | --- | --- |
| `EXECUTORS` | `claude-code` | `() -> Executor` |
| `SANDBOXES` | `local`, `sbx` | a `SandboxKind` |
| `WORKBENCHES` | `terminal`, `orca` | `() -> Workbench` |
| `TRACKERS` | `github`, `gitlab`, `local` | a `TrackerKind` |
| `PULL_REQUESTS` | `github`, `gitlab` | a `PullRequestsKind` |

Adding a kind to a port takes one `register` call in `wiring`.
`tests/test_wiring.py` registers a fake kind of every port and runs it through `factory check` and `factory run`.

- The project file names the kinds: `executor`, `sandbox.kind`, `workbench` and `code_host`.
  `wiring.kinds()` lists the registries' names, and `config.load_project` takes them as `Kinds` and rejects any other name, so `config` imports no adapter.
  A Run has one Executor, so a Stage, Describer or Decider may set `executor` only to the project's.
- A `SandboxKind` owns its settings.
  The `sandbox` block of the project file keeps `kind`, and the kind's `fields` go to its own `parse`, which reads them into the settings type of that kind (`SbxSettings` for `sbx`, `LocalSettings` for `local`) and reports each field it rejects.
  `make` builds the Sandbox from those settings, so a new kind is one module and one `register` call, and `sandbox.py` names no field of any kind.
  `config` reads the block into a `SandboxChoice` of the kind name and its settings.
  A field that another kind owns gives `sandbox.template applies only to kind sbx`.
- A `SandboxKind` declares `enforces_egress`.
  A kind that lists `egress` among its fields but cannot enforce it makes `factory check` fail with `sandbox.egress cannot be enforced by kind <name>`, instead of ignoring the field.
- An Executor declares what it needs from a Sandbox in `needs()`, and the Sandbox only applies it.
  `Needs` lists the hosts it calls (`egress`), the `env` it reads, the `credentials` its proxy holds and the `agent` it runs as.
  `sbx create` takes that `agent`, `claude` for `ClaudeCodeExecutor`, and `SbxSandbox` refuses to create a Sandbox for an Executor that names none.
  `ClaudeCodeExecutor` declares `api.anthropic.com` and the `CLAUDE_CODE_OAUTH_TOKEN` placeholder that the `sbx` proxy replaces, so `sbx.py` names no Anthropic host and no Claude variable.
  A `Credential` names the service the proxy holds, and `SbxSandbox.check` refuses a machine without it.
  A Sandbox without a proxy, such as `local`, leaves the credential's variable to the host.
- The `sandbox` block of every kind takes `env`, a mapping of variable names to strings that every `exec` in the Sandbox carries, Probes included.
  The factory's own `factory.yaml` sets `UV_PROJECT_ENVIRONMENT` there, so `sbx.py` names no Python tool.
  `factory check` rejects a name that is no variable name or starts with `FACTORY_`, a value that is no string, and an `env` that sets the variable of the Executor's credential.
- `Machine.sandbox_of(project)` makes the Sandbox and calls `Sandbox.require` with the Executor's `needs()` merged with the project's `env`.
  `SbxSandbox` merges them with its own `egress` and opens the policy for the union.
  `RunStarted` journals `sandbox_egress` and `sandbox_env`, the hosts and the variable names, never the values.
- `executor` defaults to `claude-code`, so a project file with `executor: claude-code` and one without it run the same.
- A `TrackerKind` is picked by the Spec link: each kind that has an `owns` rule (`github` by its host, `gitlab` by its `/-/issues/` path) claims its links, and the kind without one (`local`) takes the rest.
  A kind may also bring a `preflight` and `warnings` for its CLI.
- `Machine` still replaces the pick as a whole in tests: `Machine.executor`, `Machine.sandbox_for` and `Machine.workbench` win over the registries when set.

## The ports of a Run

`Ports` in `run_context` bundles the Executor, Sandbox, Workbench and Clock that `wiring` picks for a Run.
`cli` builds it once, and the Orchestrator and every phase take it whole, next to the `RunContext`.
It holds the ports only, so a module that takes it imports nothing about Attempts.
The Tracker and `PullRequests` come apart from it, because a Run picks them from the Spec link and the git remotes.

## Landing a Run: the Tracker and PullRequests

The `Tracker` holds the Spec and the Tickets, and `PullRequests` lands the Run's Integration branch as a PR.
They are two ports because the Spec and the code can live on different hosts, such as a Spec in a folder with the code on GitHub.
`Tracker` has no `pull_requests()`.

- A `PullRequestsKind` has a `serves` rule on a git remote URL, as `accepts` does for one repository, and a `make` that builds the `PullRequests` from that URL.
  `github` serves `github.com`, and `gitlab` serves a host whose name has the label `gitlab`, such as `gitlab.com`.
- `Machine.pull_requests_of` picks them from the git remotes of the Target project, not from the Tracker.
  A remote that points at the repository of the Spec wins, so a Run whose Spec and code share a host behaves as before.
  Otherwise `origin` wins, then the first remote by name that a kind serves.
- The project file field `code_host` names the kind when the remote URL does not tell, such as a self-hosted GitLab at `git.example.com`.
  The kind then reads the host and the project from the remote URL.
- With no remote that a kind serves, a Spec on GitHub or GitLab still lands in its own repository, so the landing fails and names the remote to add.
  A `LocalTracker` records the PR as a file in a `pull_requests` folder of the Spec.
- `factory check` reports a project whose git remotes no kind serves and names `code_host`.
- The Trackers keep the rest of the host: the CLI preflight, the closing keywords, and the comments and labels.
- An Executor owns its model names and efforts: `default_model` is what an Attempt runs when the Pipeline names none, `model_problem` tells why a name cannot run, and `effort_problem` tells why an effort cannot run.
  `config` declares the three as the protocol `Checks`, which the Executor implements, takes the Executors as `Kinds.checks` and checks every `model` and `effort` of the Pipeline, including those of `escalate`, the Describer and Deciders, at `factory check`.

## The settings of a Fresh agent

`AgentSettings` in `pipeline` holds what a Fresh agent runs with: `model`, `effort`, `profile`, `attempts`, `timeout`, `stall` and `executor`, and a Stage's `escalate`.
It is declared once and travels whole:

- `config` reads every block of a Fresh agent (`defaults`, a Stage, a Decider, `describer`, `ci_fix`) into it, and the field lists of those blocks come from `dataclasses.fields(AgentSettings)` plus the fields each block adds.
- `Stage`, `Decider`, `Describer` and `CiFix` hold it as `settings`, and `AgentWork` is a Role, the settings and the node's inputs.
- `for_round(counted)` applies `escalate`, so an Attempt runs with the settings of its Round.
- `AttemptRequest` carries the settings of the Round to the Executor, and only the adapter reads the fields it passes to its agent.
- `fingerprint_of` in `fingerprint` turns them into the flat `model` and `effort` of `AttemptStarted`, and `metrics.FINGERPRINT` and `stats.GROUPS` take their keys from it, so old Journals still read.

A new setting is a field of `AgentSettings`, its parse in `config._PipelineLoader._settings` and its use in the adapter.

## Open problems

### `PullRequestFolder` is no `PullRequests`

- `Machine.pull_requests_of` returns `PullRequests | PullRequestFolder`.
- `PullRequestFolder` has `record` instead of `open`, `accepts` and the CI methods.
- `run_start`, `landing` and `ci_wait` each branch on `isinstance(pull_requests, PullRequestFolder)`.
- A new way to land without a code host would add a fourth branch to each.
- Direction: make the folder a `PullRequests` kind whose `open` writes the file and whose CI reports `absent`, so the engine sees one port.

### Two ways to pick an adapter

- `Machine.executor`, `Machine.sandbox_for` and `Machine.workbench` replace the registries when a test sets them.
- `tests/harness.py` uses them, while `tests/test_wiring.py` registers fake kinds.
- With `Machine.executor` set, `kinds()` takes that one Executor as the `Checks` of every Executor kind, so the harness never checks a model through a registered kind.
- Direction: register the harness fakes as kinds too, and drop the three fields from `Machine`.

### Adapter classes in port modules

- `LocalSandbox`, `TerminalWorkbench` and `OrcaWorkbench` live in the modules of their ports.
- See Known exceptions in `dependencies.md`.
- Direction: move each into its own module of the adapters group, so `ADAPTER_CLASSES` can go.
