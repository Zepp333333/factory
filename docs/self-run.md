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

- `LocalSandbox` runs the agent on the host, straight in the Workspace.
  The agent can reach everything the owner's user can.
  Watch the Run while it goes.
- `ClaudeCodeExecutor` starts `claude -p` with `--permission-mode bypassPermissions`, because a headless agent has nobody to approve its edits.
  With `LocalSandbox` the agent runs any tool and any shell command on the host without asking.
  Nothing but the owner's own user account limits what it can read, change or send over the network.
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

## Give the agent a token

Each Run gives the agent its own `CLAUDE_CONFIG_DIR`, so the agent does not see the owner's `claude` login.
The agent signs in with `CLAUDE_CODE_OAUTH_TOKEN`, and without it every Attempt fails with `auth`.

```sh
claude setup-token
export CLAUDE_CODE_OAUTH_TOKEN='<token>'
```

Paste the token on one line.
Export it in the terminal that runs `factory run`.

## Check the contract with Claude Code

From the repository root, on an up-to-date `main`:

```sh
FACTORY_CONTRACT=1 uv run --locked pytest tests/contract
```

All three tests must pass.
A failed init test means `claude` loads more than the Agent kit, and the Run would stop every Attempt with `agent_config`.
A `401` means the token is broken or revoked.

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
