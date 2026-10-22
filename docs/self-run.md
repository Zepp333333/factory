# Self run

The factory builds itself with Pipeline `self`, declared by `factory.yaml` at the repository root.

## Pipeline `self`

```
implement -> ruff -> mypy -> pytest -> merge -> done
               \       |        /
                `---> fix <----'      (3 Rounds, opus from Round 2)
```

- `implement` and `fix` share Role `implementer` in `pipelines/self/`.
- Gates `ruff`, `mypy` and `pytest` run kind `command` with `uv run --locked`.
- A failed Gate sends the Ticket to `fix`, and `fix` goes back to `ruff`.
- `fix` that runs out of Rounds, a failed Stage and a lost Probe go to `abandoned`.
- Tickets run one at a time, so `merge` always fast-forwards.

## Risks the owner accepts

- Each Ticket runs in its own `sbx` Sandbox, a microVM that sees only the paths the factory mounts.
  It writes the Workspace, the Ticket's directory in the Run and the agent's config directory, and reads the shared `.git`.
  The Ticket's directory holds its earlier Artifacts, so the agent can delete or replace them, though not the Spec or the Ticket's text.
- `ClaudeCodeExecutor` starts `claude -p` with `--dangerously-skip-permissions`, because a headless agent has nobody to approve its edits.
  The agent runs any tool and any shell command inside the Sandbox without asking.
- The Sandbox reaches only `api.anthropic.com` and the hosts in `sandbox.egress`, here PyPI.
  Anything in the Sandbox, the agent or the code it runs, can spend the owner's subscription through the `sbx` proxy.
- The owner reviews the Integration branch in an ordinary PR before it reaches `main`.

## Install a stable factory

A Run must not execute the code it changes, so the `factory` command comes from `main`, never from a working checkout.

```sh
uv tool install --reinstall git+ssh://git@github.com/Zepp333333/factory@main
factory --help
```

The repository is private, so the install needs git access to GitHub.
Use the SSH URL above, or run `gh auth setup-git` and install from `https://github.com/Zepp333333/factory`.
Reinstall after every merge to `main` that the next Run should use.

## Set up `sbx` once

Install Docker Sandboxes and sign in to Docker:

```sh
brew trust docker/tap && brew install docker/tap/sbx
sbx login
```

Deny every host the factory does not allow per Sandbox:

```sh
sbx policy init deny-all
```

Give `sbx` the subscription with one `/login` in any Sandbox, then remove that Sandbox:

```sh
sbx run --name factory-login claude
# in claude: /login, then /exit
sbx rm -f factory-login
```

`sbx secret ls` now lists `anthropic (oauth configured)`.
`factory run` checks both and refuses to start without them.
The OAuth stays on the host, and the agent in the Sandbox gets only a placeholder.
`sbx reset` deletes the OAuth, and the next Run needs a new `/login`.

## Check the contract with Claude Code and `sbx`

From the repository root, on an up-to-date `main`:

```sh
FACTORY_CONTRACT=1 uv run --locked pytest tests/contract
```

The tests in `tests/contract/test_sbx.py` create one Sandbox and must all pass.
A failed init test means `claude` loads more than the Agent kit, and the Run would stop every Attempt with `agent_config`.
The subscription test in `tests/contract/test_claude_code.py` runs `claude` on the host and needs `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`, and it skips without one.

## Write a Spec

A `LocalTracker` Spec is a folder outside the repository:

```
~/factory-specs/<spec>/
  spec.md             # starts with "# Title"
  tickets/<id>.md     # optional front matter: title, blocked_by
```

Pick one or two small Tickets from GitHub Issues and copy their text with acceptance criteria.

## Run

From the repository root, on an up-to-date `main`:

```sh
factory check
factory run ~/factory-specs/<spec>
factory status <run-id>
```

`factory run` prints the Run id and its Integration branch.
An abandoned Ticket keeps its Workspace in `$FACTORY_HOME/workspaces/<run-id>/<ticket>` for the owner to pick up.
`FACTORY_HOME` defaults to `~/.local/share/software-factory`.

## Land the result

```sh
git push -u origin <integration-branch>
gh pr create --base main --head <integration-branch>
```

## Record findings

Write the findings of each Run to `docs/research/self-run-<date>.md`:

- where the agent got stuck
- what the Brief lacked
- which Attempts and Gates failed, and why
- what the engine lacks

File a GitHub issue for each finding that needs work.
