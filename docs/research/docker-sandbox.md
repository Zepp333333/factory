# Claude Code on a subscription in a Docker Sandbox

Issue: #4, map: #1.
Date: 2026-10-19.
Sources: primary only (documentation from Anthropic and Docker, git, developer repositories).
The owner's credentials were not read; the mechanisms are described from the documentation.
Prototype #56 checked the conclusions that the documentation did not confirm directly.
Its results replace the earlier notes, and the answers and verification commands are in `docs/research/sbx-prototype.md`.

## Short answer

- A subscription works in a container in two documented ways: an interactive `/login` inside the container (credentials are written to `~/.claude/.credentials.json` or `$CLAUDE_CONFIG_DIR`) or a one-year token from `claude setup-token`, passed as `CLAUDE_CODE_OAUTH_TOKEN`.
- For non-interactive parallel runs in a plain container the right way is `CLAUDE_CODE_OAUTH_TOKEN`: it needs no browser and does not depend on the host's Keychain.
- In `sbx` this token does not work; only the `sbx` OAuth gives the subscription, after one `/login` in any Sandbox (prototype #56).
- Docker Sandboxes (`sbx`) is the strongest ready-made implementation: a microVM, deny-by-default egress by domain, a proxy that injects credentials, and its own Docker daemon.
- The main hole in Docker Sandboxes for our model: a host worktree is mounted without the main `.git`, and the agent inside cannot run git operations.
- This is fixed by mounting the shared `.git` of the main repo as an extra workspace at the same absolute path, or by moving all git operations to the Orchestrator on the host.
- Prototype #56 confirmed the first way: with `.git` read-only, `git status`, `git diff` and `git log` work in the Sandbox.
- Recommendation for v1: the main Sandbox implementation is `sbx` with a host worktree, the Orchestrator runs git operations on the host, and authentication goes through the `sbx` OAuth after a one-time `/login` by the owner.
- Fallback implementation: a plain Docker container (modeled on Anthropic's devcontainer) with a bind mount of the worktree and the shared `.git` at the same paths, and egress through an `--internal` network plus a proxy allowlist.
- container-use does not fit v1: it is experimental, its last release is v0.4.2 from 2025-08-19, and Claude Code itself stays on the host.

## 1. How Claude Code stores and picks subscription credentials

### Where the credentials live

Source: [Authentication - Credential management](https://code.claude.com/docs/en/authentication#credential-management).

- On macOS the credentials live in the encrypted macOS Keychain.
- If the Keychain refuses a write (for example, it is locked in an SSH session), Claude Code writes the login to `~/.claude/.credentials.json` with mode `0600`.
- On Linux the credentials always live in `~/.claude/.credentials.json` with mode `0600`.
- When `CLAUDE_CONFIG_DIR` is set, the `.credentials.json` file lives in that directory, and the Keychain entry on macOS is tied to that directory.
- Consequence: subscription credentials from a macOS host cannot simply be mounted into a Linux container, because on the host they are in the Keychain, not in a file.
- The devcontainer documentation separately advises against mounting host secrets into a container and prefers short-lived or narrowly scoped tokens ([devcontainer](https://code.claude.com/docs/en/devcontainer)).

### Order in which credentials are picked

Source: [Authentication precedence](https://code.claude.com/docs/en/authentication#authentication-precedence).

1. Cloud provider (`CLAUDE_CODE_USE_BEDROCK` and the like).
2. `ANTHROPIC_AUTH_TOKEN`.
3. `ANTHROPIC_API_KEY`.
4. `apiKeyHelper`.
5. `CLAUDE_CODE_OAUTH_TOKEN` - a long-lived OAuth token from `claude setup-token`.
6. Anthropic profile and federation credentials.
7. Subscription OAuth credentials from `/login`.

Important for the Sandbox: if `ANTHROPIC_API_KEY` ends up in the environment by accident, in `-p` mode it is always used and overrides the subscription.
The Sandbox must explicitly not pass through `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`.

### `claude setup-token`

Source: [Generate a long-lived token](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token).

- The command runs the same browser OAuth flow as `/login` and prints a token valid for one year.
- The token is not saved anywhere; it goes into `CLAUDE_CODE_OAUTH_TOKEN`.
- It requires a Pro, Max, Team or Enterprise plan and uses the subscription.
- The token can only make model requests: Remote Control and claude.ai connectors do not work, local MCP servers do.
- `--bare` mode does not read `CLAUDE_CODE_OAUTH_TOKEN`, so the Orchestrator must not start the agent with `--bare`.
- The documentation names this way directly for CI and scripts where a browser login is not available.
- The devcontainer documentation advises the same token for carrying authentication between Codespaces ([devcontainer](https://code.claude.com/docs/en/devcontainer#persist-authentication-and-settings-across-rebuilds)).

### `/login` inside a container

- In a container the browser often cannot reach the local callback; then the code from the browser is pasted into the terminal at the `Paste code here if prompted` prompt ([Authentication](https://code.claude.com/docs/en/authentication#log-in-to-claude-code)).
- A login from `/login` expires and is renewed; Claude Code warns three days before it expires, and non-interactive sessions stop after it expires ([Renew an expiring login](https://code.claude.com/docs/en/authentication#renew-an-expiring-login)).
- To keep the login across rebuilds you need a named volume on `~/.claude` plus `CLAUDE_CONFIG_DIR` set to the same path, otherwise `~/.claude.json` with the OAuth account lives outside the volume ([devcontainer](https://code.claude.com/docs/en/devcontainer#persist-authentication-and-settings-across-rebuilds)).

### Which hosts the subscription needs

Source: [Network access requirements](https://code.claude.com/docs/en/network-config#network-access-requirements).

- `api.anthropic.com` - model requests, feature flags, telemetry.
- `claude.ai` - claude.ai account authentication.
- `platform.claude.com` - exchange, refresh and revocation of OAuth tokens, also needed for the claude.ai login.
- Optional: `downloads.claude.ai` (auto-update), `registry.npmjs.org`, Datadog hosts for telemetry.
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` turns off nonessential traffic, and `DISABLE_AUTOUPDATER=1` turns off auto-update ([devcontainer](https://code.claude.com/docs/en/devcontainer#enforce-organization-policy)).
- The sandbox runtime documentation says that `claude.ai` and `platform.claude.com` are needed for OAuth sign-in and refresh, and can be removed with an API key ([sandbox-environments](https://code.claude.com/docs/en/sandbox-environments#sandbox-runtime)).
- The minimal agent allowlist in `sbx` is only `api.anthropic.com`, because the proxy on the host refreshes the OAuth (prototype #56).
- For a plain container with `CLAUDE_CODE_OAUTH_TOKEN` the list was not checked.

## 2. Docker Sandboxes (`sbx`)

### What it is

- A free standalone Docker product; Docker Desktop is not needed ([install](https://docs.docker.com/ai/sandboxes/install/), [sandbox-environments](https://code.claude.com/docs/en/sandbox-environments#virtual-machine)).
- Requirements on macOS: Sonoma 14+ and Apple silicon, and signing in to a Docker account with `sbx login` ([install](https://docs.docker.com/ai/sandboxes/install/)).
- Install: `brew trust docker/tap && brew install docker/tap/sbx`.
- Each Sandbox is a microVM with hypervisor-level isolation and its own Docker daemon ([architecture](https://docs.docker.com/ai/sandboxes/architecture/)).
- The old pages for the `docker sandbox run` command (Docker Desktop 4.50+) now return 404; the current interface is `sbx`.
- Start: `sbx run claude <path>`; by default `claude --dangerously-skip-permissions` starts inside ([Claude Code agent](https://docs.docker.com/ai/sandboxes/agents/claude-code/)).
- The prompt is passed after `--`: `sbx run --name t1 claude -- "..."`; there is `-d` for detached and `--rm` for a one-off Sandbox ([usage](https://docs.docker.com/ai/sandboxes/usage/)).
- Base image: `docker/sandbox-templates:claude-code`; you can build your own on top of it ([Claude Code agent](https://docs.docker.com/ai/sandboxes/agents/claude-code/)).
- The Sandbox does not read the host config `~/.claude`; only the project-level config from the working directory is available ([Claude Code agent](https://docs.docker.com/ai/sandboxes/agents/claude-code/)).

### Subscription credentials in `sbx`

Source: [Claude Code agent](https://docs.docker.com/ai/sandboxes/agents/claude-code/), [credentials](https://docs.docker.com/ai/sandboxes/configuration/credentials/).

- For a subscription: without an API key, run `/login` inside Claude Code in the Sandbox.
- For an API key: `sbx secret set anthropic`; the key is stored in the host's macOS Keychain, and the Sandbox sees only the sentinel `proxy-managed`.
- The proxy on the host replaces the sentinel with the real value after the request leaves the microVM, so the real credential never enters the VM ([architecture](https://docs.docker.com/ai/sandboxes/architecture/#follow-an-authenticated-request)).
- For OAuth, the table on the credentials page says that sign-in happens on the host and the token does not enter the Sandbox.
- Further down, the same page says that Claude Code asks for a login interactively inside the Sandbox, so the documentation contradicts itself.
- The documentation does not say where the OAuth token physically lives after `/login` in the Sandbox.
- Prototype #56 showed that `sbx` stores it on the host as the service secret `anthropic (oauth configured)`, and in the VM `~/.claude/.credentials.json` holds only a placeholder.
- The agent's state is kept in the VM until `sbx rm` ([architecture](https://docs.docker.com/ai/sandboxes/architecture/)), but the OAuth is global, so `/login` is needed once, not for every Sandbox.
- For the Orchestrator a one-time `/login` by the owner is acceptable; the Orchestrator only checks that the OAuth is there.
- There are custom secrets: `sbx secret set-custom --host api.example.com --env API_KEY`; inside the Sandbox the variable holds the placeholder `sbx-cs-<rand>`, and the proxy replaces it anywhere in a request to the given host ([credentials](https://docs.docker.com/ai/sandboxes/configuration/credentials/)).
- The hypothesis that `sbx secret set-custom --host api.anthropic.com --env CLAUDE_CODE_OAUTH_TOKEN` would give the subscription without the token entering the VM was not confirmed (prototype #56).
- Without the service OAuth the proxy strips any authorization from requests to `api.anthropic.com`, so a token in a plain environment variable does not work either.
- `sbx secret set anthropic` with this token stores it as an API key, and the API answers `invalid x-api-key`.

### Network

Sources: [security](https://docs.docker.com/ai/sandboxes/security/), [defaults](https://docs.docker.com/ai/sandboxes/security/defaults/), [local access controls](https://docs.docker.com/ai/sandboxes/governance/access-controls/local/).

- All outgoing TCP goes through the proxy on the host, the policy is deny by default, ICMP is blocked, and UDP is experimental only.
- Presets: `allow-all`, `balanced` (a baseline allowlist for common services), `deny-all`; set with `sbx policy init <preset>`.
- In `balanced` and `deny-all`, requests that are not allowed land in `sbx policy approval ls`, but the request itself gets HTTP 403 at once and does not wait (prototype #56).
- Rules: `sbx policy allow network "api.anthropic.com,*.npmjs.org"`, `sbx policy deny network ...`, `--sandbox <name>` for a rule on one Sandbox, `sbx policy check network <host>` to check.
- Wildcards, ports (`host:22`) and rules by HTTP method and path (`--method GET --path '/repos/org/**'`) are supported.
- Deny rules beat allow rules, and org-level rules beat local ones.
- The documentation warns that the baseline allowlist contains broad wildcards such as `*.googleapis.com`, and the extra ones are worth removing.
- A sensible setup for a v1 Ticket: `deny-all` plus an explicit list (`api.anthropic.com` and the registries the project needs, for Python `pypi.org` and `files.pythonhosted.org`), see prototype #56.

### Workspace and git worktree

Sources: [usage](https://docs.docker.com/ai/sandboxes/usage/), [git workflows](https://docs.docker.com/ai/sandboxes/workflows/git/), [security](https://docs.docker.com/ai/sandboxes/security/).

- Direct mode (the default): the workspace is mounted read-write at the same absolute path as on the host, and changes are visible at once.
- Clone mode (`--clone`): the host repo is mounted read-only at `/run/sandbox/source`, the agent works in a private clone, and the host fetches branches through the remote `sandbox-<name>`.
- Clone mode is rejected if it is started from a linked worktree rather than from the main checkout.
- Host worktree: Docker documents `git worktree add ... && sbx run claude ../my-feature-work`.
- In that case only the worktree directory is mounted, the `.git` file points to an unreachable path, and the agent cannot commit, branch or even look at `git status`.
- Docker itself recommends committing and pushing from the host in this mode.
- Extra paths are mounted after the main one: `sbx run claude ~/project-a ~/shared-libs:ro`, each at the same absolute path; `:ro` makes it read-only.
- Risk of direct mode: git hooks, CI configs, Makefiles and `package.json` scripts changed by the agent run on the host, and hooks in `.git/` are not visible in `git diff`.

## 3. How a worktree is laid out and what needs to be mounted

Sources: [git-worktree DETAILS](https://git-scm.com/docs/git-worktree#_details), [gitrepository-layout](https://git-scm.com/docs/gitrepository-layout).

- The root of a linked worktree has a `.git` file with the line `gitdir: <path>`; by default the path is absolute, for example `/path/main/.git/worktrees/test-next`.
- That private directory holds the per-worktree files: `HEAD`, `index` and others.
- The `commondir` file in it points to the shared `$GIT_COMMON_DIR` (the main `.git`); a relative path is resolved from `$GIT_DIR`.
- Objects, refs (except `refs/bisect`, `refs/worktree`, `refs/rewritten`), `config` and `hooks` come from the shared `.git`.
- The file `worktrees/<id>/gitdir` stores the absolute path back to the worktree's `.git` file.
- The option `git worktree add --relative-paths` or `worktree.useRelativePaths=true` makes the links relative, but turns on `extensions.relativeWorktrees` and breaks compatibility with older git.
- Conclusion: for git to work inside the Sandbox, the worktree and the main `.git` must be mounted so that the paths from the `.git` file and `commondir` resolve.
- The simplest way is to mount both at the same absolute paths; then the absolute links work without `--relative-paths`.
- In `sbx` this gives `sbx run claude /path/wt /path/main/.git`; in plain Docker it is two bind mounts with identical paths.
- Prototype #56 confirmed this for `sbx`, including with `/path/main/.git:ro`.
- For commits the shared `.git` must be read-write: objects and refs are written there.
- Write access to the shared `.git` is access to `hooks/` and `config`, that is, a path to running code on the host at the owner's next git call.
- Claude Code's built-in sandbox solves this the same way: it allows writes to the shared `.git` for a linked worktree but forbids `hooks/` and `config` ([sandboxing](https://code.claude.com/docs/en/sandboxing)).
- Such a targeted ban can be repeated with bind mounts by laying a read-only mount over `hooks/` and `config`.
- In `sbx` this works: `.git` read-write with `.git/hooks:ro` and `.git/config:ro` on top allows commits and forbids writes to hooks and config (prototype #56).
- A simpler and more robust option for v1: the agent in the Sandbox runs no git write operations, and the Orchestrator makes commits, branches and merges on the host.

## 4. Anthropic's devcontainer

Sources: [devcontainer](https://code.claude.com/docs/en/devcontainer), [reference](https://github.com/anthropics/claude-code/tree/main/.devcontainer), [init-firewall.sh](https://github.com/anthropics/claude-code/blob/main/.devcontainer/init-firewall.sh).

- Claude Code is installed by the Dev Container Feature `ghcr.io/anthropics/devcontainer-features/claude-code:1.0`; to pin a version you need `npm install -g @anthropic-ai/claude-code@X.Y.Z` and `DISABLE_AUTOUPDATER=1`.
- Reference container: a Dockerfile, `devcontainer.json` with a volume for the config (`claude-code-config-${devcontainerId}`) and `init-firewall.sh`.
- The firewall runs on `iptables` and `ipset`, requires `NET_ADMIN` and `NET_RAW`, the default policy is DROP, and outgoing traffic that is not allowed gets REJECT.
- Allowed: GitHub ranges from `api.github.com/meta`, A records of `registry.npmjs.org`, `api.anthropic.com`, `sentry.io`, `statsig.com` and the VS Code hosts, the host's local /24, and DNS and SSH to any address.
- Domains are resolved to IPs once at startup, so the allowlist is IP-based and goes stale when a CDN changes its IPs.
- The list has no `claude.ai` and `platform.claude.com`, so refreshing the `/login` token through this firewall will probably fail.
- Prototype #56 checked only `sbx`; this conclusion remains an open question for the fallback implementation on our own image.
- Documentation: `--dangerously-skip-permissions` is acceptable in a container as non-root; the CLI refuses to start as root.
- The documentation warns directly: with `--dangerously-skip-permissions` the container does not stop a malicious project from exfiltrating everything inside it, including the credentials in `~/.claude`.
- Devcontainer is a convention for IDEs, not an orchestration API; for the Orchestrator it is useful as a model Dockerfile and firewall.

## 5. Egress allowlist in plain Docker

- Docker Engine has no built-in domain egress allowlist; by default containers can reach the outside ([Docker networking](https://docs.docker.com/engine/network/)).
- There is `--network none` (full isolation) and `--internal` networks without external access ([Docker networking](https://docs.docker.com/engine/network/)).
- Hence two working patterns.
- Pattern A: iptables+ipset inside the container, like `init-firewall.sh`; the downside is `NET_ADMIN` on the container and an IP-based allowlist.
- Pattern B: the agent container is only on an `--internal` network, and the only way out is a proxy container on both networks with a domain allowlist; the agent gets `HTTPS_PROXY` and `HTTP_PROXY`.
- Claude Code honors `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, but not SOCKS ([network-config](https://code.claude.com/docs/en/network-config#proxy-configuration)).
- Pattern B is closer to what `sbx` and Claude Code's built-in sandbox do: checking by hostname at the proxy, without TLS inspection.
- The weak spot of pattern B: tools that ignore the proxy variables (ssh, database drivers) simply do not work, which is rather a plus.
- Inside the container you can additionally turn on Claude Code's built-in Bash sandbox; in an unprivileged container it needs `enableWeakerNestedSandbox`, which noticeably weakens isolation ([sandboxing](https://code.claude.com/docs/en/sandboxing)).

## 6. dagger/container-use

Sources: [README](https://github.com/dagger/container-use), [docs](https://container-use.com/introduction), [agent integrations](https://container-use.com/agent-integrations).

- This is an MCP server on Dagger: each agent gets its own container and its own git branch, and git worktrees are used inside.
- Setup: `claude mcp add container-use -- container-use stdio`; for strict mode, `--allowedTools` only on `mcp__container-use__environment_*`.
- Claude Code itself then runs on the host, and only the MCP tools run in the container, so the subscription credentials stay on the host.
- Downside: the isolation rests on the agent not using the built-in Bash and Edit, not on an OS boundary.
- Status "stability-experimental", last release v0.4.2 from 2025-08-19, last push 2026-09-21 (per the GitHub API).
- Network and secrets are not described in the documentation.
- Not recommended for v1, but the idea "Claude on the host, tools in the container" is useful as an alternative Sandbox implementation if the subscription in a container turns out to be a problem.

## 7. Comparison for Orchestrator v1

| Criterion | Docker Sandboxes (`sbx`) | Own Docker container | container-use |
| - | - | - | - |
| Isolation boundary | microVM | container (shared Docker VM kernel) | container for MCP tools only |
| Subscription | `sbx` OAuth after one `/login`, custom secret does not work | `CLAUDE_CODE_OAUTH_TOKEN` in env | credentials on the host, unchanged |
| Credentials visible to the agent | no, neither for an API key nor for OAuth | yes | no |
| Egress | domain-based deny-by-default, `sbx policy` | do it yourself: iptables or internal network+proxy | not described |
| Worktree with shared `.git` | worktree and shared `.git` as an extra mount at host paths | full control of bind mounts | its own worktrees inside |
| Automation | `sbx` CLI, detached, `--rm` | `docker run`, full control | MCP |
| Maturity | active Docker product | standard Docker | experimental |

## 8. Recommendation

- The Sandbox abstraction in the Spec should take as input: the Workspace (worktree) path, the shared `.git` path, the authentication method, the egress allowlist, and the agent command.
- First implementation: `sbx`, direct mode on a host worktree, `deny-all` plus an explicit allowlist, subscription through the `sbx` OAuth after a one-time `/login`.
- The Orchestrator runs git write operations (commit, branch, merge) on the host, which matches the documented Docker flow for a host worktree and closes the risk of hooks in the shared `.git`.
- If the agent needs read-only git (`git diff`, `git log`), the shared `.git` is mounted with `:ro` (prototype #56).
- Second implementation (fallback and CI): our own Docker image modeled on the reference devcontainer, the worktree and `.git` at the same paths, an `--internal` network plus a proxy allowlist.
- Prototype ticket #56 checked the subscription, git with an extra `.git` mount, the minimal allowlist and parallel Sandboxes on one OAuth; the results are in `docs/research/sbx-prototype.md`.
- Subscription limits for parallel Sandboxes are shared across the account; this is a separate question for the "Models and cost" map.

## 9. What carries over to a Sandbox on a separate macOS user

- Credentials: each user has their own Keychain, so the owner's subscription is not visible in it, and that is good.
- The same `CLAUDE_CODE_OAUTH_TOKEN` works there too, as does `/login` as that user ([Authentication](https://code.claude.com/docs/en/authentication)).
- If the process is started through `su` or SSH and the Keychain is locked, `/login` falls back to `~/.claude/.credentials.json` with `0600` ([Authentication](https://code.claude.com/docs/en/authentication#credential-management)); a token in env does not have this problem.
- Worktree: paths on one disk are the same for all users, so the absolute links of the `.git` file and `commondir` work without mounting.
- OS permissions are needed: the Sandbox user must be able to write to its worktree and to the shared `.git`, otherwise the Orchestrator commits, as in the Docker option.
- The risk of hooks and `config` in the shared `.git` is the same, and so is the solution: git write operations belong to the Orchestrator.
- Egress: macOS has no per-user domain firewall out of the box.
- A domain allowlist comes from Claude Code's built-in sandbox (Seatbelt, a proxy, `sandbox.network.allowedDomains`, `strictAllowlist`), but it restricts only Bash commands ([sandboxing](https://code.claude.com/docs/en/sandboxing)).
- To cover the whole process, including file tools, MCP and hooks, there is `@anthropic-ai/sandbox-runtime` on the same Seatbelt; it is in beta research preview ([sandbox-environments](https://code.claude.com/docs/en/sandbox-environments#sandbox-runtime)).
- A separate user gives an OS file boundary, and Seatbelt through sandbox-runtime gives the network; together they replace the microVM boundary, but are weaker than it.
- What does not carry over: `sbx policy`, Docker's credential proxy, the microVM, its own Docker daemon.
- Conclusion for the abstraction: the Sandbox interface should describe the allowlist and the way the token is passed declaratively, and the implementation decides whether `sbx policy`, a proxy container or sandbox-runtime enforces them.
