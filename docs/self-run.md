# Self run

The factory builds itself with Pipeline `self`, declared by `factory.yaml` at the repository root.

## Pipeline `self`

```
implement -> sync -> ruff -> mypy -> pytest -> merge -> done
              |  ^      \       |        /
     conflict |  |       `---> fix <----'     (3 Rounds, opus from Round 2, back to sync)
              v  |
            resolve                          (2 Rounds, back to sync)
```

- `implement` and `fix` share Role `implementer`, and `resolve` has Role `reconciler`, all in `pipelines/self/`.
- `sync` brings the Integration branch's tip into the Ticket branch, so the Gates judge the tree that `merge` fast-forwards to.
- A text conflict in `sync` leaves the markers in the Workspace and sends the Ticket to `resolve`.
  The agent only edits the files, and the host makes the merge commit once no markers are left.
- Gates `ruff`, `mypy` and `pytest` run kind `command` with `uv run --locked`.
- A failed Gate sends the Ticket to `fix`, and `fix` and `resolve` go back to `sync`.
- `fix` or `resolve` that runs out of Rounds, a failed Stage and a lost Probe go to `abandoned`.
- `factory.yaml` sets no `max_parallel`, so Tickets run one at a time and `sync` finds no conflict.
- After the last merge the Describer, Role `describer` with skill `pr`, writes the PR's title and body, and the engine appends the `Closes` lines.
  The text is `pull_request.md` in the Run directory.

## Risks the owner accepts

- Each Ticket runs in its own `sbx` Sandbox, a microVM that sees only the paths the factory mounts.
  It writes the Workspace, the Ticket's directory in the Run and the agent's config directory, and reads the shared `.git`.
  The Ticket's directory holds its earlier Artifacts, so the agent can delete or replace them, though not the Spec or the Ticket's text.
- `ClaudeCodeExecutor` starts `claude -p` with `--dangerously-skip-permissions`, because a headless agent has nobody to approve its edits.
  The agent runs any tool and any shell command inside the Sandbox without asking.
- The Sandbox reaches only `api.anthropic.com` and the hosts in `sandbox.egress`, here PyPI.
  Anything in the Sandbox, the agent or the code it runs, can spend the owner's subscription through the `sbx` proxy.
- `gh` on the host reads the Spec and opens the PR with the owner's GitHub access, and no Sandbox gets that access.
- The owner reviews the Integration branch in the PR the Run opens, and merges it into `main` by hand.

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

The tests in `tests/contract/test_github.py` need a throwaway repository that the owner's `gh` can write:

```sh
FACTORY_CONTRACT=1 FACTORY_GITHUB_REPO=<owner>/<scratch-repo> uv run --locked pytest tests/contract/test_github.py
```

They publish a Spec with sub-issues and blocked-by links there and close every issue at the end.
The PR test opens a PR from a branch of its own, then closes the PR and deletes the branch.

## Publish a Spec

The Spec is an issue in GitHub Issues of the Target project:

- the Spec's own issue holds its title and text
- each Ticket is a native sub-issue of it, with acceptance criteria in its text
- an order between Tickets is a native blocked-by link

A closed sub-issue is done, and a Run without `--tickets` skips it.
A closed blocker is done too, and the Run does not wait on it.

`gh` on the host must be signed in with access to the repository:

```sh
gh auth status
```

## Run

From the repository root, on an up-to-date `main`:

```sh
factory check
factory run https://github.com/Zepp333333/factory/issues/<spec>
factory run https://github.com/Zepp333333/factory/issues/<spec> --tickets 28,30
factory status <run-id>
```

- `factory run` reads the Spec once at the start, so later edits and closed issues do not reach the Run.
- `--tickets` takes only those sub-issues, so a large Spec can go in parts.
- A number that is not a sub-issue of the Spec stops `factory run` before the Run starts.
- An open blocker outside the Run stops it too, because its Ticket would build on code that does not exist yet.
- A Ticket's branch is `factory/<run-id>--<issue number>`.

`factory run` prints the Run id and its Integration branch.
An abandoned Ticket keeps its Workspace in `$FACTORY_HOME/workspaces/<run-id>/<ticket>` for the owner to pick up.
`FACTORY_HOME` defaults to `~/.local/share/software-factory`.

## Land the result

A Run that merged at least one Ticket pushes its Integration branch to the remote of the Spec's repository and opens a PR into `base`.
The PR closes the merged Tickets with `Closes #N` and lists the Tickets that did not land with the reason.
`factory run` prints the PR's link last.

A failed push or PR leaves the Run's state alone.
The journal records it as `LandingFailed`, and `factory run` prints the commands that finish the landing by hand:

```
Land it by hand:
  git push origin factory/<run-id>
  gh pr create --repo Zepp333333/factory --base main --head factory/<run-id> --title <title> --body-file <run-dir>/pull_request.md
```

A Spec folder of a `LocalTracker`, as the engine's tests use, gets no push and no PR.

## Record findings

Write the findings of each Run to `docs/research/self-run-<date>.md`:

- where the agent got stuck
- what the Brief lacked
- which Attempts and Gates failed, and why
- what the engine lacks

File a GitHub issue for each finding that needs work.
