# Software Factory

Software Factory takes a Spec from your issue tracker, splits the work into Tickets, and runs each Ticket through a Pipeline of fresh Claude Code agents.
Tickets run in parallel, each in its own git worktree and sandbox.
Finished Tickets merge onto one integration branch, and the Run opens a single pull request for you to review.

The Orchestrator is plain deterministic code, not an LLM.
Models do the work inside Stages and help with Decisions, but only the Orchestrator changes the state of a Run.

> Status: early and experimental.
> The factory builds itself, and its interfaces still change.

## How it works

```
Spec (GitHub issue)
  └─ Tickets (sub-issues, ordered by blocked-by links)
       └─ each Ticket runs its Pipeline in its own Workspace and Sandbox
            Stage (fresh agent) -> Gates (deterministic checks) -> Decisions -> merge
                 └─ Integration branch -> one PR into base
```

- **Spec** is a GitHub issue, and its sub-issues are the **Tickets**.
- **Pipeline** is a YAML state machine of Stages, Gates, Decisions and Operations.
- **Stage** starts a fresh agent with only a Brief of pointers to its declared inputs.
- **Gate** runs a command such as tests or a linter and passes or fails, with no model involved.
- **Decision** picks one outcome from a closed list, made by code, a model or you.
- **Journal** records every event of a Run in SQLite, so a Run can resume after a crash.

[The guide](docs/guide/README.md) explains the factory from scratch with diagrams, and shows how to adapt it to your project.
`CONTEXT.md` defines every term, and `docs/adr/` explains why the design looks the way it does.

## Requirements

- macOS with Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- git and the [GitHub CLI](https://cli.github.com/), signed in with access to the Target project
- [Claude Code](https://claude.com/claude-code) with a subscription
- Docker Sandboxes (`sbx`) for isolating agents
- Optionally Orca as a Workbench, or a plain terminal

## Install

```sh
uv tool install git+https://github.com/Zepp333333/factory@main
factory --help
```

## Quick start

1. Copy `examples/tdd-review/factory.example.yaml` to your project as `factory.yaml` and fill in `vars`.
2. Publish a Spec as a GitHub issue with Tickets as sub-issues.
3. Check the setup and start a Run from the project's root:

```sh
factory check
factory run https://github.com/<owner>/<repo>/issues/<spec>
factory status <run-id>
```

`docs/self-run.md` walks through the full setup, including the one-time `sbx` login, using the factory's own repository as the example.

## Commands

| Command | What it does |
| --- | --- |
| `factory check` | Validates the project file and its Pipeline |
| `factory run` | Runs a Spec, or only `--tickets`, through the Pipeline |
| `factory status` | Shows a Run from the Journal |
| `factory decide` | Answers a Decision that waits for you |
| `factory cancel` | Abandons a Ticket |
| `factory override` | Replaces the outcome of a Ticket's Decision |
| `factory limits` | Changes parallelism, round limits and the `throttle` brake of a live Run |
| `factory resume` | Continues an orphaned Run from the Journal |
| `factory finish` | Ends an orphaned Run and opens its PR |
| `factory clean` | Lists, and with `--yes` removes, the Workspaces, Sandboxes and branches left by Runs no longer running |
| `factory attach` | Follows the terminal of a Ticket's latest Attempt |

Run `factory <command> --help` for the options.

## Repository layout

| Path | Contents |
| --- | --- |
| `src/factory/` | The engine and the CLI |
| `src/factory/kit/` | The Agent kit every fresh agent gets |
| `src/factory/library/` | The `factory` Layer of the Library: Pipelines, Roles, prompts and schemas for any Target project |
| `.factory/` | The project Layer of this repository: Pipeline `self` and the extensions that set it apart |
| `examples/` | Example project files for Target projects |
| `tests/` | Unit and end-to-end tests, plus opt-in contract tests in `tests/contract/` |
| `docs/guide/` | The user guide: goals, glossary, how a Run works, Pipelines and customization |
| `docs/adr/` | Architecture decision records |
| `CONTEXT.md` | The domain glossary |

## Development

The `Makefile` wraps every development command, and `make` lists them.
Each tool runs through `uv run --locked`, so a local run matches CI.

```sh
make install        # create .venv and install the locked dependencies
make check          # lint, types, tests and factory check, as CI runs them
make fix            # fix lint findings and format the code
```

| Command | What it does |
| --- | --- |
| `make venv` | Recreates `.venv` with Python 3.14, or `PYTHON=3.12` |
| `make install` | Installs the locked dependencies |
| `make lock` | Updates `uv.lock` after a manual edit of `pyproject.toml` |
| `make upgrade` | Upgrades every dependency |
| `make upgrade-package PKG=ruff` | Upgrades one dependency |
| `make add PKG=httpx` | Adds a runtime dependency, `make add-dev` adds a dev one |
| `make lint` | Checks lint and formatting with ruff |
| `make fix` | Fixes lint findings and formats the code |
| `make types` | Type-checks with mypy |
| `make test` | Runs the tests in parallel |
| `make test ARGS="tests/test_cli.py -k status"` | Runs only some tests |
| `make test-serial` | Runs the tests in one process, for debugging |
| `make test-contract` | Runs the contract tests against real services |
| `make factory-check` | Validates `factory.yaml` and its Pipeline |
| `make clean` | Removes caches, `make distclean` also removes `.venv` |

`CODING_STANDARDS.md` describes the conventions, and CI runs `make lint`, `make types`, `make test` and `make factory-check` on every pull request.

## Security model

Agents run with `--dangerously-skip-permissions` inside a sandbox that reaches only the Claude API and the hosts the project allows.
GitHub access stays on the host, and you merge the final PR by hand.
`docs/self-run.md` lists the risks in detail.
