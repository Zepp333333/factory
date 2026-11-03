# 7. Adding an adapter

This page is for someone who adds a vendor to the factory: another agent harness, Sandbox, Workbench, tracker or code host.
Such a vendor is one adapter of a port, which the project file picks by its kind name.
Adding it takes one module under `src/factory/adapters/`, one registration in `src/factory/wiring.py`, and one fake in the contract tests of its port.

## The five ports

| Port | What it does | Registry in `wiring` | Kinds today | Contract tests |
| --- | --- | --- | --- | --- |
| Executor | Runs a Fresh agent for one Attempt | `executors` | `claude-code` | `tests/contracts/test_executor_contract.py` |
| Sandbox | Runs the agents and Probes of a Ticket in isolation | `sandboxes` | `local`, `sbx` | `tests/contracts/test_sandbox_contract.py` |
| Workbench | Creates the Workspaces and the Attempt terminals the owner watches | `workbenches` | `terminal`, `orca` | `tests/contracts/test_workbench_contract.py` |
| Tracker | Reads the Spec and projects the Run onto its Tickets | `trackers` | `github`, `gitlab`, `local` | `tests/contracts/test_tracker_contract.py` |
| PullRequests | Opens the Run's PR and reads its CI | `pull_requests` | `github`, `gitlab` | `tests/contracts/test_pull_requests_contract.py` |

Each port is a `Protocol` in `src/factory/ports/`, and its docstrings say what every member must do.

## 1. Write the adapter module

Put the adapter in a module of its own under `src/factory/adapters/`, such as `adapters/codex.py`.
It implements the port's `Protocol` and defines its `Kind` from `factory.ports.kind`:

- `make(settings, tools)` builds the adapter, with the `gh` and `glab` commands of the Machine in `tools`.
- `fields` and `parse` read the fields of the port's block in the project file besides `kind`, such as `cpus` of `sandbox: {kind: sbx, cpus: 4}`.
- `check(settings, context)` returns the `Problems` that `factory check` and the start of a Run report.
- `picks(link)` is the rule of a Tracker or PullRequests kind: the Spec links or git remote URLs it serves.
- `enforces_egress` tells whether a Sandbox holds back every host that `egress` does not list.

`sbx.KIND`, `terminal_workbench.ORCA` and `github.TRACKER` are examples.
No other module of `src/factory` imports the adapter or names its vendor.

## 2. Register the kind

Add one line to `src/factory/wiring.py`:

```python
REGISTRIES.executors.register("codex", codex.KIND)
```

The kind name is what the project file names, as `executor: codex` or `executor: {kind: codex}`.

## 3. Fake its host CLI for the contract

Each contract module runs every kind its port's registry holds, so the new kind runs through it at once.
It fails with `the executors kind 'codex' has no fake in the contract of its port` until you give the kind a fake:

1. Write a fake of the vendor's CLI in `tests/`, such as `tests/fake_codex.py`, next to `fake_gh.py`, `fake_glab.py`, `fake_sbx.py` and `fake_orca.py`.
   It keeps its state in a directory and records its calls, as they do.
2. Put it on the `PATH` under the CLI's own name in the `host` fixture of `tests/contracts/hosts.py`, so the kind's own `make` reaches the fake.
3. Add the kind to the `FAKED` table of the port's contract module.
   The Executor, Sandbox and Workbench tables name the host CLI the kind runs.
   The Tracker and PullRequests tables hold a `Bench` that publishes the same Spec or sets the same CI on the kind's fake, and reads back what the fake holds.

`tests/contracts/test_kinds_contract.py` checks that every table names exactly the kinds that `wiring` registers.

## 4. Run the contract

```
uv run --locked pytest tests/contracts
```

The kind is done when it passes every test of its port's contract:

- Executor: an Attempt polls to the agent's output and its cost, it writes its Attempt events next to its raw stream (ADR 0013), a failed Attempt ends with its failure class, `kill` stops a waiting agent, `needs()` merges with the project's, the default model passes the model check, and `assemble` builds the agent kit with the Library's skills.
- Sandbox: `require` refuses a variable set twice, `check` passes, `exec` runs in the Workspace with its env and tells its exit code, a command reads its mounts, `kill` stops a command and its children, `reap` leaves nothing behind, and `stop` and `start` work.
- Workbench: a Workspace is there from its creation to its removal, `remove_workspace` refuses one with edits, `mark_stage` takes the Stage of a Workspace, and an Attempt terminal keeps the rendered Attempt events in its log and is gone once closed.
- Tracker: the kind's `make` returns a `Tracker` with the roles it serves, `SpecSource` and `Closing` always and `TicketBoard` and `IssueDesk` when it can (ADR 0012), and the contract skips the tests of a role it leaves None. `read_spec` snapshots the Spec and its Tickets with their blockers, a comment lands on its Ticket, a label is added once and removed, a Tracker made with Ticket ids reads only those and refuses one whose blocker it does not take, an issue is created and found by its labels, and the footer closes exactly the merged Tickets and shows the rest by the kind's `reference`.
- PullRequests: the kind accepts the remote it serves, opens a PR and edits its body, tells its head, and reads CI as `none`, `pending`, `passed` or `failed`.

Behaviour that only the new vendor has, such as the flags of its CLI, gets a test of its own beside the contract, through the `factory` CLI with the harness in `tests/harness.py`.
Each new test adds its row to `tests/inventory.csv`, as `CODING_STANDARDS.md` asks.
