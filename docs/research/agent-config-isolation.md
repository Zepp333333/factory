# A Stage agent's own set of skills, hooks and instructions without the owner's `~/.claude`

Issue: #18, map: #1.
Research date: 2026-10-20.
CLI version checked: Claude Code 2.1.304 (locally, `claude --help` and experiments with `claude -p`).
Related notes: `docs/research/claude-code-subscription-exec.md` (#2), `docs/research/docker-sandbox.md`.

## Short answer

- `--settings` alone does not isolate: it lays keys on top of the settings files, but lists (hooks, permissions) merge, and the RTK hook from `~/.claude/settings.json` keeps working.
- The owner can be cut off at the CLI level with the flag `--setting-sources`: without the `user` source, none of these load: `~/.claude/settings.json` (hooks, `enabledPlugins`, output style), `~/.claude/CLAUDE.md`, `~/.claude/skills`, `~/.claude/agents`, user MCP servers and plugin sync with claude.ai.
- The factory's set is passed explicitly: `--plugin-dir` (skills, subagents, hooks, MCP in one plugin), `--settings` (hooks, permissions, service keys), `--mcp-config` together with `--strict-mcp-config`, `--append-system-prompt-file` instead of the user CLAUDE.md.
- All of this was checked locally: with `--setting-sources ""` and the factory's flags, the agent sees only the factory's plugin, its MCP server and its hooks, and the owner's hooks do not start.
- `--setting-sources` does not touch `~/.claude.json`, auto memory or the transcript directory in `~/.claude/projects`, and login is still taken from the owner's Keychain.
- Full isolation comes from `CLAUDE_CONFIG_DIR=<factory directory>`: all the "home" config, including `.claude.json`, auto memory and transcripts, moves there, but the login from the owner's Keychain is no longer found, so authentication through `CLAUDE_CODE_OAUTH_TOKEN` is needed.
- `CLAUDE_CODE_OAUTH_TOKEN` is compatible with all these flags except `--bare`: bare mode does not read OAuth at all.
- In `sbx` the host's `~/.claude` is not visible anyway, but the same launch line is needed there too: arguments after `--` are appended to `claude --dangerously-skip-permissions`, and the factory's set is mounted read-only or baked into the template.
- Recommendation: one and the same factory launch profile for host and Sandbox: `CLAUDE_CONFIG_DIR` per Run, `CLAUDE_CODE_OAUTH_TOKEN`, `--setting-sources project` (or `""` for hermetic Stages), `--plugin-dir <factory-kit>`, `--settings <factory-settings.json>`, `--strict-mcp-config --mcp-config <stage-mcp.json>`, `--append-system-prompt-file <stage-instructions.md>`.

## 1. What exactly leaks from the owner

The structure of `~/.claude` on the owner's machine (only file and key names, no secret contents):

- `~/.claude/settings.json`: keys `hooks` (14 events, including `PreToolUse` with matcher `Bash` and the command `rtk hook claude`), `enabledPlugins` (10 plugins from several marketplaces), `extraKnownMarketplaces`, `outputStyle`, `statusLine`, `model`, `effortLevel`, `autoMode`.
- `~/.claude/CLAUDE.md` with the import `@RTK.md`, `~/.claude/skills/`, `~/.claude/agents/`, `~/.claude/commands/`, `~/.claude/output-styles/`, `~/.claude/plugins/`.
- `~/.claude.json`: login, user MCP servers, trust decisions per project ([settings](https://code.claude.com/docs/en/settings)).

A baseline `claude -p` run in a clean directory with a single `CLAUDE.md` (experiment `baseline`, the `system/init` event from `--output-format stream-json`) showed:

- 11 plugins, 127 skills, 9 subagents, 8 MCP servers (plugin ones, user-level `asana`, `atlassian`, `pycharm` and the claude.ai connectors `Claude Docs`, `Google Drive`).
- The owner's hooks on `SessionStart`, `UserPromptSubmit`, `Stop`.
- The owner's output style `Plain Prose`.
- In the model's context: `# My agent instructions`, `# RTK - Rust Token Killer (imported via @RTK.md)` and only then the project's `CLAUDE.md`.
- Auto memory is written to `~/.claude/projects/<path>/memory/`.

## 2. How settings merge

- The order, from highest to lowest: managed, `--settings`, `.claude/settings.local.json`, `.claude/settings.json`, `~/.claude/settings.json` ([settings precedence](https://code.claude.com/docs/en/settings#settings-precedence)).
- A key from `--settings` overrides the same key from the files, and keys it omits keep their values from the files ([CLI reference](https://code.claude.com/docs/en/cli-reference)).
- Lists do not override but merge: "When you set the same list key ... in more than one file, Claude Code combines the lists instead of picking one" ([settings](https://code.claude.com/docs/en/settings#lists-merge-instead-of-overriding)).
- Consequence: `--settings` cannot remove an owner's hook, it can only add one of its own.
- `disableAllHooks` turns off all hooks at once, including the factory's hooks, and `allowManagedHooksOnly` works only from managed settings ([settings reference](https://code.claude.com/docs/en/settings-reference)).
- `enabledPlugins` merges by plugin id: the value comes from the highest source that mentions the id ([plugin loading](https://code.claude.com/docs/en/plugins/loading#find-where-a-plugin-is-enabled)).
- Environment variables are not a level of this stack; precedence is decided pairwise for each variable-key pair ([settings](https://code.claude.com/docs/en/settings#settings-precedence)).

## 3. Flags and variables

### `--setting-sources`

- A comma-separated list of the sources `user`, `project`, `local` ([CLI reference](https://code.claude.com/docs/en/cli-reference)).
- The Agent SDK describes the same mechanism (`settingSources`) in more detail: `user` is `~/.claude/settings.json`, `~/.claude/CLAUDE.md`, `~/.claude/rules/*.md`, `~/.claude/skills/`, `~/.claude/commands/`, `~/.claude/agents/`; `project` is `.claude/settings.json`, hooks, CLAUDE.md and rules in cwd and its parents, and the project's skills, commands and subagents; `local` is `CLAUDE.local.md` and `.claude/settings.local.json` ([SDK: Claude Code features](https://code.claude.com/docs/en/agent-sdk/claude-code-features)).
- It does not control: managed settings, `~/.claude.json`, auto memory, claude.ai MCP connectors under a claude.ai login, `sandbox.credentials` from `~/.claude/settings.json` ([same page](https://code.claude.com/docs/en/agent-sdk/claude-code-features#what-settingsources-does-not-control)).
- Plugins synced from claude.ai do not load with `--setting-sources` without `user` ([plugin loading](https://code.claude.com/docs/en/plugins/loading#sign-in-requirements-for-terminal-sync)).
- Experiment `ss-project` (`--setting-sources project,local`): 0 MCP servers (both the user-level ones from `~/.claude.json` and the claude.ai connectors were gone), only the built-in plugins `cc-plugin-*@builtin`, 19 bundled skills, built-in subagents, not a single owner hook, output style `default`, and only the project's `CLAUDE.md` in the context.
- In this mode auto memory is still written to the owner's `~/.claude/projects/...`.
- Experiment `nobundled` (`--setting-sources ""`): the project's `CLAUDE.md` did not load either, so the empty list cuts off the project too.

### `--plugin-dir` and `CLAUDE_CODE_PLUGIN_DIRS`

- They load a plugin from a directory or `.zip` for this session only; the flag is repeatable, and a directory with several plugins can be passed ([CLI reference](https://code.claude.com/docs/en/cli-reference)).
- `CLAUDE_CODE_PLUGIN_DIRS` does the same through the environment, with absolute paths and `:` as the separator ([env vars](https://code.claude.com/docs/en/env-vars)).
- Such a plugin gets the id `<name>@inline` and is enabled unless the manifest says `defaultEnabled: false` ([plugin loading](https://code.claude.com/docs/en/plugins/loading#find-where-a-plugin-is-enabled)).
- It overrides an installed marketplace plugin of the same name and loses only to managed settings ([name conflicts](https://code.claude.com/docs/en/plugins/loading#name-conflicts)).
- The plugin loads in place, without being copied to the cache ([in-place plugins](https://code.claude.com/docs/en/plugins/loading#in-place-and-copied-plugins)).
- In experiment `factory` the plugin's skill showed up as `factory-kit:factory-hello`, and its `hooks/hooks.json` on `SessionStart` ran.

### `--settings`

- A file or inline JSON, applied above user/project/local and below managed ([settings](https://code.claude.com/docs/en/settings)).
- The place for the factory's hooks, permissions, `disableBundledSkills`, `autoMemoryEnabled: false`, `syncClaudeAiSkills: false`, `syncClaudeAiPlugins: false`.
- In experiment `factory` the hook from `--settings` ran together with the plugin's hook.
- `{"disableBundledSkills": true}` in experiment `nobundled` removed the bundled skills: what remained were `factory-kit:factory-hello` and the skills of the built-in plugins (`design`, `doctor`, `plugin-authoring`).

### `--mcp-config` and `--strict-mcp-config`

- `--mcp-config` loads MCP servers from JSON files or strings, and `--strict-mcp-config` ignores all other MCP configurations ([CLI reference](https://code.claude.com/docs/en/cli-reference)).
- `strictMcpConfig` also turns off the claude.ai connectors, which otherwise load under a claude.ai login regardless of `settingSources` ([SDK: Claude Code features](https://code.claude.com/docs/en/agent-sdk/claude-code-features#what-settingsources-does-not-control)).
- In experiments `factory` and `empty` exactly one server, `factory-echo`, is visible.

### Instructions instead of the user CLAUDE.md

- `--append-system-prompt-file` appends a file to the default system prompt, keeping the tool guidance and safety rules ([CLI reference: system prompt flags](https://code.claude.com/docs/en/cli-reference#system-prompt-flags)).
- `--system-prompt-file` replaces the prompt entirely and drops the default instructions, which is worse for a coding agent.
- `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` turns off all CLAUDE.md files, including the project one ([env vars](https://code.claude.com/docs/en/env-vars)).
- `--agents <json|file>` defines subagents right in the launch line, but subagents in the factory's plugin are easier to version.

### `CLAUDE_CONFIG_DIR`

- "Override the configuration directory (default: `~/.claude`). All settings, session history, and plugins are stored under this path" ([env vars](https://code.claude.com/docs/en/env-vars)).
- Experiment `cfgdir` with an empty directory: the CLI created its own `.claude.json`, `projects/`, `sessions/`, `backups/` in it, auto memory went there too, and nothing from the owner's `~/.claude` loaded.
- The same run ended with `Not logged in · Please run /login`: on macOS the Keychain entry is tied to the config directory ([authentication](https://code.claude.com/docs/en/authentication#credential-management)).
- It can be set only from the shell, user or managed settings; project and local settings ignore it ([env vars](https://code.claude.com/docs/en/env-vars)).
- `CLAUDE_CODE_PROJECT_DIR_NAME` together with it sets the name of the transcript and auto memory directory instead of the one derived from the path ([env vars](https://code.claude.com/docs/en/env-vars)).

### What does not fit

- `--bare` (`CLAUDE_CODE_SIMPLE=1`): cuts off everything, but authentication is "strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings (OAuth and keychain are never read)" (`claude --help`), so the subscription does not work.
- `--safe-mode`: experiment `safe` showed that together with the owner's config it also turns off the factory's hooks, MCP and CLAUDE.md, while the owner's output style stays; it is a debugging mode, not an isolation mode.
- `--restricted`: loads only managed and `--settings`, but removes Bash and other code execution tools unless they are named in `--tools`, and forbids `bypassPermissions` ([CLI reference](https://code.claude.com/docs/en/cli-reference)); for an implementation agent these are needless restrictions.
- `--disable-slash-commands` turns off all skills, including the factory's skills.

## 4. Compatibility with the subscription (`CLAUDE_CODE_OAUTH_TOKEN`)

- The token from `claude setup-token` takes precedence over the Keychain and is used for the whole session ([env vars](https://code.claude.com/docs/en/env-vars)).
- It is an environment variable, so it works with any `CLAUDE_CONFIG_DIR`, `--setting-sources`, `--plugin-dir`, `--settings`, `--mcp-config`.
- The only incompatible mode is `--bare`, see above and `docs/research/claude-code-subscription-exec.md`.
- With the token, claude.ai skills and plugins do not sync and claude.ai connectors do not load ([skills](https://code.claude.com/docs/en/skills), [plugin loading](https://code.claude.com/docs/en/plugins/loading#sign-in-requirements-for-terminal-sync), [SDK: Claude Code features](https://code.claude.com/docs/en/agent-sdk/claude-code-features)); for the factory this is a plus: the owner's account does not mix in its own set.
- Without the token and without `CLAUDE_CONFIG_DIR` (the `--setting-sources` variant on top of the owner's Keychain) everything works, but the agent shares `~/.claude.json`, auto memory and the transcript directory with the owner.
- The token was not checked locally: generating a `setup-token` requires an action by the owner, and reading existing credentials is forbidden.

## 5. Compatibility with the Sandbox on `sbx`

- The Sandbox does not read the host's user config; only the project config of the working directory is available ([sbx: Claude Code](https://docs.docker.com/ai/sandboxes/agents/claude-code/)).
- By default `claude --dangerously-skip-permissions` runs inside, and arguments after `--` that start with a flag are appended to those flags ([same page](https://docs.docker.com/ai/sandboxes/agents/claude-code/)).
- So the factory's flags are passed as `sbx run claude <workspace> <factory-kit>:ro -- --setting-sources project --plugin-dir <factory-kit> ...`, and the extra path is mounted at the same absolute path ([sbx usage](https://docs.docker.com/ai/sandboxes/usage/)).
- The alternative to mounting is a custom template on top of `docker/sandbox-templates:claude-code` with the factory's plugin inside; for marketplace plugins there is `CLAUDE_CODE_PLUGIN_SEED_DIR` ([env vars](https://code.claude.com/docs/en/env-vars)).
- Inside the VM, `~/.claude` is the image's config, not the owner's, so `CLAUDE_CONFIG_DIR` is needed there only to keep the launch line uniform.
- The subscription in the Sandbox still goes through `CLAUDE_CODE_OAUTH_TOKEN` or `/login` inside; the question of injecting the token through the `sbx` proxy stays open in `docs/research/docker-sandbox.md`.
- The Workspace's project `.claude/` and `CLAUDE.md` are visible in the Sandbox the same way as on the host, so the choice of `--setting-sources project` or `""` is the same for both implementations.

## 6. Recommendation

One factory launch profile for host and Sandbox, built by the engine for every Stage launch:

```bash
CLAUDE_CONFIG_DIR=<factory-state>/runs/<run-id>/claude \
CLAUDE_CODE_OAUTH_TOKEN=<from factory secret store> \
CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 \
claude -p \
  --setting-sources project \
  --plugin-dir <factory-repo>/agent-kit \
  --settings <factory-repo>/agent-kit/settings.json \
  --strict-mcp-config --mcp-config <stage-mcp.json> \
  --append-system-prompt-file <stage-instructions.md> \
  --output-format stream-json --verbose ...
```

- `agent-kit` is an ordinary Claude Code plugin in the factory's repository: `skills/`, `agents/`, `hooks/hooks.json`, and `.mcp.json` if needed; this is exactly the "own list of skills that the factory decides on itself", versioned together with the engine.
- The factory's `settings.json`: the factory's hooks and permissions, `disableBundledSkills: true`, `autoMemoryEnabled: false`, `syncClaudeAiSkills: false`, `syncClaudeAiPlugins: false`.
- `--setting-sources project` by default: the agent sees the `CLAUDE.md`, skills and hooks of the Target project, as a developer of the project would see them; `local` is excluded, because those are the owner's personal settings.
- For Stages that do not need the project config or for which it is dangerous (for example, reviewing someone else's code), the Pipeline sets `--setting-sources ""`.
- `CLAUDE_CONFIG_DIR` per Run separates `.claude.json`, transcripts and auto memory from the owner and gives the engine a predictable place for `--resume` and log analysis.
- If `CLAUDE_CONFIG_DIR` is not needed for some reason (for example, an early prototype on the owner's Keychain), `--setting-sources` plus `--strict-mcp-config` already cut off the owner's hooks, plugins, skills, subagents, MCP and CLAUDE.md; only `~/.claude.json`, auto memory and transcripts remain.
- A check in the engine's CI: parse the `system/init` event and compare `plugins`, `skills`, `mcp_servers`, `agents` with the Stage's expected set, and see hooks through `--include-hook-events`.

## 7. Remaining risks and open questions

- The built-in plugins `cc-plugin-*@builtin` and their skills (`design`, `doctor`, `plugin-authoring`) remain with any set of flags, and there is no documented way to remove them, apart from `skillOverrides` for non-plugin skills.
- The host's managed settings always apply; the owner's machine has no managed directory, but the factory must check this on other machines.
- If the Workspace lies inside the main checkout (for example, `.claude/worktrees/...`), the `project` source also picks up the `CLAUDE.md` of the parent directories; the Workspace is better placed outside the Target project's tree.
- Project-scope plugins and MCP of the Target project require trust, which `-p` does not grant ([plugin loading](https://code.claude.com/docs/en/plugins/loading#plugins-shared-through-a-repository)); for trusted own projects this has to be taken into account when preparing `CLAUDE_CONFIG_DIR` **[to verify]**.
- That `CLAUDE_CODE_OAUTH_TOKEN` works with an empty `CLAUDE_CONFIG_DIR` follows from the documentation but was not checked locally **[to verify]**.

## Experiments

All runs: `claude -p --model haiku --max-turns 1 --output-format stream-json --verbose --include-hook-events --no-session-persistence` in an empty directory with a single `CLAUDE.md`, a test plugin `factory-kit` (a skill, hooks on `SessionStart` and `PreToolUse`), a test `--settings` with a hook on `SessionStart`, a test MCP `factory-echo`.

| Run | Flags | Plugins | MCP | Owner's hooks | CLAUDE.md in context |
| :- | :- | :- | :- | :- | :- |
| `baseline` | none | 11 of the owner + builtin | 8 | yes | user + RTK + project |
| `ss-project` | `--setting-sources project,local` | builtin | 0 | no | project |
| `factory` | `ss-project` + `--settings` + `--plugin-dir` + `--mcp-config` + `--strict-mcp-config` | `factory-kit` + builtin | `factory-echo` | no, factory hooks yes | project |
| `empty` | like `factory`, but `--setting-sources ""` and `--append-system-prompt-file` | `factory-kit` + builtin | `factory-echo` | no, factory hooks yes | through append |
| `nobundled` | `--setting-sources ""` + `disableBundledSkills` + `autoMemoryEnabled: false` | `factory-kit` + builtin | 0 | no | no |
| `safe` | `--safe-mode` + factory flags | list present, no skills | 0 | no, no factory hooks either | no |
| `cfgdir` | empty `CLAUDE_CONFIG_DIR` | builtin | 0 | no | the model did not answer: `Not logged in` |
