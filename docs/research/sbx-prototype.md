# `sbx` prototype: subscription, mounts, egress and exec

Issue: #56, parent: #21, consumers: #45 (`SbxSandbox`) and #53 (lost Probe).
Date: 2026-10-22.
Resolves the **[to verify]** marks from `docs/research/docker-sandbox.md`.
The scripts lived in `prototypes/sbx/` on the `prototype/sbx` branch of the original repository, were never merged into `main` and were not carried over.
Below, "VM" means the microVM in which `sbx` runs a Sandbox.

## Bench

| What | Value |
|---|---|
| Host | macOS, Apple silicon |
| `sbx` | v0.49.1 |
| `claude` on the host | 2.1.308 |
| Template | `docker.io/docker/sandbox-templates:claude-code-docker`, digest `sha256:4a94d9144a70df744fd9247e0c365a14cfb97024afab8db7597c8ba3e0637642` |
| `claude` in the template | 2.1.306, matches `src/factory/kit/builtin.json` |
| `sbx` secrets | the global service `anthropic (oauth configured)`, set up through `/login` in `claude` inside a Sandbox |
| Global policy | `deny-all` plus `allow api.anthropic.com` |
| Worktree | a throwaway `git worktree add --detach` of the factory repository |

All paths in the Sandbox match the host paths.
The agent in the VM runs as `agent` (uid 1000) with `sudo` and the `docker` group.
All scripts read `SBXP_ROOT`, see `prototypes/sbx/README.md` on that branch.

## Short answer

- The subscription in a Sandbox works only through the `sbx` service OAuth, which is set up once through `/login` in `claude` inside any Sandbox.
- After that the proxy injects the OAuth into any request to `api.anthropic.com`, and new Sandboxes no longer need `/login`.
- A token from `claude setup-token` does not give the subscription in `sbx`, whether through `set-custom`, env or `sbx secret set anthropic`.
- `claude` in the VM needs some non-empty `CLAUDE_CODE_OAUTH_TOKEN`, otherwise it does not start and reports "Not logged in".
- The real OAuth does not enter the VM, but `GH_TOKEN` does, in the format of a real GitHub token.
- Mounts at host paths work: `.git` read-only, a single file, a read-only subdirectory inside a read-write directory, and `.git/hooks` with `.git/config` read-only on top of a read-write `.git`.
- `deny-all` egress refuses at once (HTTP 403 in 10-30 ms) and only puts the request into `sbx policy approval ls`; nothing waits.
- The Probe `uv run --locked pytest -q` passes if `pypi.org` and `files.pythonhosted.org` are allowed or if the uv cache is baked into the template.
- `kill` of the `sbx exec` process on the host does **not** stop the process in the VM; a watchdog inside is needed.
- The exit code of `sbx exec` does not tell a command exit from a Sandbox death; a file with the exit code is needed.
- After a hard VM death the Sandbox neither starts nor gets removed; all you can do is abandon it.
- An idle Sandbox stops by itself after 30-40 seconds, and the next `sbx exec` brings it up in 2 seconds.

## Subscription

| Check | Answer | Command |
|---|---|---|
| `claude -p` in a new Sandbox works on the subscription without `/login` in it | yes, after one `/login` in any Sandbox | `agent.sh NAME` |
| A `setup-token` token through `set-custom` gives the subscription | no, the proxy strips authorization from requests to `api.anthropic.com` | `auth.sh NAME` without the service secret |
| A `setup-token` token through `sbx secret set anthropic` gives the subscription | no, the proxy sends it as `x-api-key`, the answer is `invalid x-api-key` | `auth.sh NAME`, `agent.sh NAME` |
| The real OAuth does not enter the VM | yes, env and `~/.claude/.credentials.json` hold only a placeholder | `secrets.sh NAME` |
| `GH_TOKEN` does not enter the VM | no, the VM has 40 characters in the GitHub token format | `secrets.sh NAME` |
| Minimal list of hosts for the subscription | `api.anthropic.com:443` | `agent.sh NAME`, then `sbx policy log NAME` |

### How a request is authorized

- `claude` with `CLAUDE_CONFIG_DIR` on an empty directory and without `CLAUDE_CODE_OAUTH_TOKEN` does not start and returns "Not logged in" at zero cost.
- With `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-bogus` the request goes through.
- With the placeholder from `sbx secret ls` the request goes through too.
- With the default `~/.claude` (where `sbx` puts `.credentials.json`) and without the variable the request goes through.
- Conclusion: the subscription comes from the `anthropic` service secret; the proxy puts its header on any request to `api.anthropic.com`.
- With the service OAuth, `auth.sh` gets the same answer for a request without authorization, with the placeholder, with a bogus Bearer and with a bogus `x-api-key`.
- The answer is 429, not 401, because the subscription serves only `claude` requests, not a bare `curl`.
- For #45 this matches its criterion "subscription from the `sbx` OAuth after a one-time `/login`".
- Any non-empty `CLAUDE_CODE_OAUTH_TOKEN` is enough for the agent, and the factory can pass it through `-e`.

### How the OAuth is set up

- `sbx secret set anthropic --oauth` does not work in local `sbx`: "anthropic OAuth cannot be started from `sbx secret set`; sign in from inside the Claude sandbox".
- The Docker documentation says the same: "If no API key is set, use the `/login` command inside Claude Code to authenticate via OAuth" ([claude-code](https://docs.docker.com/ai/sandboxes/agents/claude-code/#authentication)).
- `/login` in `sbx run --name NAME` saves the OAuth on the host as `anthropic (oauth configured)`, and it takes effect in all Sandboxes at once.
- `sbx reset` wipes this secret together with all the others; after it a new `/login` is needed.
- The factory cannot set up the subscription by itself: at startup it checks `sbx secret ls` and, if there is no OAuth, asks the owner to sign in once.

### The `setup-token` token does not work in `sbx`

Checked without the `anthropic` service secret, after `sbx reset`.

- `set-custom --host api.anthropic.com --env CLAUDE_CODE_OAUTH_TOKEN` puts the placeholder `sbx-cs-...` into the VM, but the `claude` request gets 401 "Not logged in".
- `auth.sh` shows that all four variants get 401 "x-api-key header is required", that is, the proxy strips any authorization from requests to `api.anthropic.com`.
- So a real token passed into the VM through `-e` will not get through either.
- `printf %s "$TOKEN" | sbx secret set anthropic` stores the token as an API key, the proxy sends it in `x-api-key`, and the answer is 401 "invalid x-api-key".

```bash
sbx exec NAME env CLAUDE_CONFIG_DIR=$EMPTY_DIR CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-bogus \
  claude -p "say ok" --model haiku --output-format json
```

### Risk

- Any process in the VM, including the code of a hostile repository, gets the owner's subscription through the proxy if it can reach `api.anthropic.com`.
- The real token does not enter the VM, but the subscription's limits and its usage are open to everything that runs in the Sandbox.
- This is the same boundary as for an agent with `--dangerously-skip-permissions`: the agent and the project code in one Sandbox cannot be told apart.

### Hosts

- The agent with the factory's flags went only to `api.anthropic.com:443`.
- `claude` with the default `~/.claude` also goes to `platform.claude.com`, `mcp-proxy.anthropic.com` and `mcp-gateway.docker.internal`, but works without them too.
- At startup the template contacts `ports.ubuntu.com:80`, `cli.github.com:443` and `download.docker.com:443`, and `claude` sends telemetry to `http-intake.logs.us5.datadoghq.com`; a refusal hinders neither.
- The OAuth lives on the host with the `sbx` proxy, so refreshing the token apparently does not need `claude.ai` or `platform.claude.com` from the VM; the refresh itself was not observed on the bench.

### Open questions for #45

- `GH_TOKEN` enters the VM in the format of a real GitHub token, although it is not in `sbx secret ls`.
  Where `sbx` takes it from and whether it works was not checked: egress to GitHub is closed.
  `SbxSandbox` must unset it (`env -u GH_TOKEN`) or decide explicitly that it is needed.
- How the proxy behaves when the OAuth on the host expires in the middle of an Attempt; this did not happen on the bench.

## Mounts

| Check | Answer | Command |
|---|---|---|
| Worktree read-write at the host path | yes, virtiofs `rw` | `setup.sh NAME`, `mount` in the VM |
| Shared `.git` read-only, `status`, `diff`, `log` work | yes | `git-ro.sh NAME` |
| `git commit` fails | yes, `Unable to create .../index.lock: Read-only file system` | `git-ro.sh NAME` |
| A host commit is visible in the VM at once | yes, without delay | `git-ro.sh NAME` |
| Brief inputs read-only, Attempt, `CLAUDE_CONFIG_DIR` and `$GATE_OUT` read-write | yes | `setup.sh NAME`, a write and `touch` in each directory |
| Mounting a single file or a subdirectory | yes, both | `mounts.sh NAME` |

- `.git/hooks:ro` and `.git/config:ro` on top of a read-write `.git` work: a commit from the Sandbox goes through, while a write to a hook and `git config` fail (`mounts.sh NAME`).
- So the agent can be given commits into the shared `.git` without a path to running code on the host through hooks and config.
- Mounts are set only at `sbx create`; a path cannot be added to an existing Sandbox.
- If a host directory is deleted and created again, the VM sees `Stale file handle` until the Sandbox is restarted.
- If a directory is missing at startup, `sbx` refuses to start: `422 ... workspace directory ... no longer exists on the host`.
- The default working directory of `sbx exec` is the first workspace; if it breaks, `-w /` helps.

### uv and the Workspace

- `uv run` in the VM creates `.venv` right in the host's Workspace, with `python -> /usr/bin/python3` from Linux.
- Such a `.venv` breaks `uv` on the host and leaks into the Gates that read the Workspace.
- `SbxSandbox` must set `UV_PROJECT_ENVIRONMENT` to a path inside the VM, for example `/home/agent/venv`; with it no `.venv` appears in the Workspace.

## Agent kit

| Check | Answer | Command |
|---|---|---|
| The kit loads through `--plugin-dir` with a host path | yes, `factory-kit@inline` | `agent.sh NAME` |
| init checked against `kit.required` and `builtin.json` | matches exactly | `agent.sh NAME` |
| The `claude` version in the template and how to pin it | 2.1.306, the template digest | `sbx create -t docker.io/docker/sandbox-templates@sha256:4a94... claude`, `claude --version` in the VM |

init with the factory's flags:

- plugins: `factory-kit@inline`, `cc-plugin-agents-md@builtin`, `cc-plugin-telemetry@builtin`, `cc-plugin-plugin-authoring@builtin`.
- skills: `design`, `doctor`, `plugin-authoring`.
- agents: `claude`, `Explore`, `general-purpose`, `Plan`, `statusline-setup`.
- mcp_servers: empty, `apiKeySource: none`.

What `sbx` adds on its own to `~/.claude` and `~/.claude.json`:

- The MCP server `mcp-gateway` (`http://mcp-gateway.docker.internal/mcp`, `Authorization: Bearer proxy-managed`) in user scope.
- `settings.json` with `permissions.defaultMode: bypassPermissions`, `alwaysThinkingEnabled: true`, `skipDangerousModePermissionPrompt: true`.
- The directory `skills/synced` from the shared `sbx` store (mode `--skills readonly` by default), currently empty.
- `.credentials.json` and `remote-settings.json`.
- Without the factory's flags, init shows 19 bundled skills and `mcp-gateway`.
- `CLAUDE_CONFIG_DIR`, `--strict-mcp-config` and `--setting-sources ""` remove all of this from the agent's init.
- `sbx template save` bakes `~/.claude/skills/synced` into the template.

Environment variables that `sbx` sets in the VM:

- `HTTP(S)_PROXY`, `NO_PROXY`, `JAVA_TOOL_OPTIONS`, `NODE_USE_ENV_PROXY`, `PROXY_CA_CERT_B64`.
- `CLAUDE_CODE_OAUTH_TOKEN`, `GH_TOKEN`, `MCP_SENTINEL_TOKEN_NAME=proxy-managed`, `SBX_CRED_ANTHROPIC_MODE=none`.
- `WORKSPACE_DIR`, `SBX_AGENT`, `CLAUDE_ENV_FILE=/etc/sandbox-persistent.sh` (empty).

### The `claude` version

- `sbx create` pulls the template again by default (`--pull always` for built-in agents).
- The first `create` on the bench took 80 s and changed the image from `549730947ed8` to `4a94d9144a70`, that is, the `claude` version in the Sandbox floats with the tag.
- Pinning: `-t docker.io/docker/sandbox-templates@sha256:<digest>`; `create` takes 5 s.
- A custom template from `sbx template save` also pins the version; it is started with `--pull never`.
- Auto-update of `claude` in the VM runs into egress; after an hour of work in sbxp1 the version stayed 2.1.306.
- `DISABLE_AUTOUPDATER=1` was not checked; to be safe, #45 can set it through `-e`.

## Egress

| Check | Answer | Command |
|---|---|---|
| `deny-all` with an allowlist lets `claude` through and nothing else | yes | `egress.sh NAME` |
| A request outside the allowlist is refused at once | yes, 403 in 10-30 ms | `egress.sh NAME` |
| Hosts for `uv run --locked` | `pypi.org`, `files.pythonhosted.org` | `template.sh NAME`, the first Sandbox |

- The refusal comes as HTTP 403 from the proxy; the request lands in `sbx policy approval ls` with the options `allow` and `dismiss`, but the process does not wait.
- `--locked` does not avoid `pypi.org`: the build backend `hatchling` is not in `uv.lock`, and uv looks it up through the index.
- `sbx policy allow network --sandbox NAME HOST` applies to one Sandbox, and the global policy does not change.

## Toolchain and Probe

| Check | Answer | Command |
|---|---|---|
| Template with git, Python 3.12+ and uv | `claude-code-docker`: git 2.53.0, Python 3.14.4, uv 0.13.4, Docker 29.9.2, Ubuntu 26.04 | `git --version; python3 --version; uv --version` in the VM |
| The Probe `uv run --locked pytest -q` passes | yes, 131 passed, 3 skipped | `template.sh NAME`, `exec.sh NAME $WT ... uv run --locked pytest -q` |
| Network to PyPI versus dependencies in the image | both work | `template.sh NAME` |

| Variant | Egress | First Probe | `create` |
|---|---|---|---|
| PyPI in the allowlist | `pypi.org`, `files.pythonhosted.org` | 8.6 s with downloads | 5 s by digest |
| uv cache in the template (`sbx template save`) | none | 5.9 s, no network | 3.7 s |

- A template with the cache settles the #53 question about `uv run --locked` without network in a Ticket's first Probe.
- Downside of the template: it has to be rebuilt when `uv.lock` changes, otherwise the first Probe goes to the network again and runs into egress.
- `sbx template save` needs a TTY for confirmation and suggests a nonexistent `--force` flag; without a terminal, `script -q /dev/null` helps.

## `exec` and lifecycle

| Check | Answer | Command |
|---|---|---|
| stdout and stderr to host files, exit code | yes, with the caveats below | `sbx exec NAME sh -c 'echo; echo >&2; exit 7' >o 2>e` |
| Environment variables | yes, `-e KEY=VALUE`, an empty value too | `sbx exec -e FOO=bar -e EMPTY= NAME env` |
| `exec` stays open for an hour | yes, 62 minutes, the command's code 5 came back | `sbx exec NAME sh -c 'while ...; sleep 60; done; exit 5'` |
| `kill` on the host stops the process and its children in the VM | **no**, worked around with a watchdog | `kill.sh NAME`, `kill-workaround.sh NAME` |
| Tell a command exit from a Sandbox death | by a file with the exit code | `exec.sh`, `lost.sh NAME` |
| Time of `create` and `rm`, `--cpus`, `--memory`, `DOCKER_CLI_HINTS` | see "Idling and timing" | `time sbx create`, `nproc` and `free` in the VM |
| Two Sandboxes at once on one token | yes, on short runs | `parallel.sh A B 3` |

### stdout and stderr

- Output reaches the host files line by line while the process runs.
- Exit codes 0-255 pass through undistorted, but failures of `sbx` itself give the same codes, see below.
- If the Sandbox was stopped, `sbx exec` brings it up by itself and writes `✓ sandbox NAME - started` to its stderr, that is, to the agent's `stderr.log`.
- `SbxSandbox` must bring the Sandbox up before `exec` or filter out this line.

### `kill`

- `SIGTERM`, `SIGINT`, `SIGHUP` and `SIGKILL` of the `sbx exec` process on the host leave the process and both its children in the VM alive.
- On `SIGTERM` and `SIGINT`, `sbx exec` itself exits with code 1.
- Workaround A: the command writes its PGID into the Attempt directory, and a second `sbx exec NAME kill -TERM -- -PGID` kills the group.
- Workaround B: `sbx exec -i` with stdin open and a watchdog in the VM that kills the group on stdin EOF.
- Workaround B also fires on `SIGKILL` or a crash of the Orchestrator itself, so it is better for #45.
- Only the Orchestrator itself may write to stdin; a separate holder process survives a `SIGKILL` of the Orchestrator and keeps the tree in the VM alive.
- `exec.sh` holds the FIFO open for writing on its own fd and gives `sbx exec` a separate read-only fd.
- In `sh` a background job gets its stdin from `/dev/null`, so the watchdog takes stdin through `exec 3<&0` and `cat <&3`.

### Command exit versus Sandbox death

The exit code of `sbx exec` tells nothing:

| Event | `sbx exec` code | stderr |
|---|---|---|
| The command exited with 255 | 255 | empty |
| `sbx stop` in the middle of the command | 143, and 0 with `-i` | empty |
| `sbx rm -f` in the middle of the command | 137 | empty |
| The VM process killed on the host | 255 | empty |
| No such Sandbox | 1 | `error: sandbox ... not found` |
| No such command | 127 | `OCI runtime exec failed` |

- `exec.sh` writes the command's exit code to a file in the Attempt directory.
- The file exists: the command exited with that code.
- The file is missing: the Probe is lost, and `sbx ls --json` gives the reason (`running`, `stopped` or the Sandbox is gone).
- `lost.sh` breaks `exec.sh` in four ways: `SIGKILL` of the host `sbx exec`, `SIGKILL` of the wrapper itself, `sbx stop`, and killing the VM.
- `exec.sh` recognizes all four as a loss, and after both `SIGKILL`s nothing is left in the VM.

### After a VM death

- If the VM process (`containerd-shim-nerdbox-v1`) is killed on the host, the Sandbox goes to `stopped`.
- The next start fails with `500 Internal Server Error: failed to start runtime`, and a retry does not help.
- `sbx rm -f` fails with `failed to fully delete sandbox`.
- This reproduced four times out of four.
- For #53 this means that the retry of a lost Probe runs in a new Sandbox with a new name, and the old one stays as garbage until `sbx reset` or a daemon restart.

### Idling and timing

- An idle Sandbox without an active `exec` stops after 30-40 seconds, and `sbx settings` has no setting for this.
- An active `exec` keeps the Sandbox running: an hour-long `exec` in sbxp1 was not interrupted, even though its workspace on the host was deleted and created again in the middle of the hour.
- The next `sbx exec` brings it up in 1.7-2 s; the VM boots again, but files in `~` and `/tmp` are kept.
- `sbx create` from a pinned digest takes 5 s, from a custom template 3.7-4.5 s, and with a download of a new template 80 s.
- `sbx rm -f` takes 1 s.
- `--cpus 4 --memory 8g` give `nproc` 4 and 7.x GiB in the VM.
- `DOCKER_CLI_HINTS=false` has no effect on `sbx`, and the `try: ...` hints are printed anyway; the variable matters only for `docker` calls.

### Parallel Sandboxes

- Two agents in different Sandboxes on one OAuth ran at the same time for five rounds in a row (`parallel.sh`), 4-8 s and 2-3 turns each, without errors.
- This only shows that the proxy neither serializes nor breaks parallel requests.
- Subscription limits under the load of real Attempts are not checked by this; that is a question for the "Models and cost" map.

## What else came up

- An outside process deleted the throwaway worktree right during the prototype: the directory disappeared, and `git worktree list` no longer shows it.
  Most likely this was worktree cleanup in Orca; after `git worktree lock` it did not happen again.
- `sbx settings` turns on `ssh.agentForwardingEnabled` and `browser.enabled` by default; they are worth turning off for the factory's Sandboxes.

## Changes for #45

- Subscription: the `sbx` service OAuth after a one-time `/login` inside a Sandbox, and any non-empty `CLAUDE_CODE_OAUTH_TOKEN` in the VM.
- At startup check for `anthropic (oauth configured)` in `sbx secret ls`, and do not start a Run without it.
- Remove `set-custom` and the `setup-token` token from the criteria; in `sbx` they do not give the subscription.
- `create`: the template by digest or a custom template with `--pull never`, explicit `--cpus` and `--memory`.
- Mounts: `.git` can be given read-write with `.git/hooks:ro` and `.git/config:ro` on top if the agent needs commits; otherwise `.git:ro`.
- `exec`: `-i` with a stdin watchdog, `setsid`, the exit code in a file in the Attempt directory, `-w` on the Workspace, as in `exec.sh`.
- `exec` unsets `GH_TOKEN` and sets `UV_PROJECT_ENVIRONMENT` outside the Workspace.
- Bring the Sandbox up before `exec`, so that `sbx` does not write its line into `stderr.log`.
- Egress: `pypi.org` and `files.pythonhosted.org` for Python projects, or a template with the cache.
- `DOCKER_CLI_HINTS=false` has no effect on `sbx`; the criterion has to be reworded or removed.
- Removing a Sandbox must survive a `500` from `sbx rm` and must not block the Run.
