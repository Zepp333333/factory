# How a Python engine runs Claude Code on a subscription

Issue: #2, map: #1.
Research date: 2026-10-19.
CLI version checked: Claude Code 2.1.303 (locally, `claude --help`).
Codex CLI is not installed locally, so facts about it come only from the documentation.

## Short answer

The engine runs Claude Code as a child process `claude -p` with `--output-format stream-json` (or `json`), using the owner's subscription login (OAuth from `/login` or `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`).
The Python Agent SDK (`claude-agent-sdk`) does the same thing: it runs the same Claude Code binary as a subprocess and talks to it over the same stream-json protocol.
So the choice between `claude -p` and the SDK is a choice of wrapper, not of authentication method or limits.
An interactive session in tmux or an Orca terminal is fine for watching and for manual takeover, but not as a control channel: it has no structured result and no exit code per step.
Anthropic's documentation directly supports using the subscription from your own script on your own machine for yourself (`setup-token` "for CI pipelines, scripts"), but Pro/Max limits are sized for "ordinary, individual usage", and a parallel run eats them up quickly.
What is forbidden is something else: embedding claude.ai login in a product for third parties, modifying the binary and intercepting OAuth tokens outside the standard flow.
Codex CLI fits in as a second executor on the same scheme: `codex exec --json`, `--output-schema`, `codex exec resume`, `-m`.

## 1. Ways to run Claude Code

### 1.1. `claude -p` (headless)

`-p`/`--print` starts a session without the TUI and exits the process after the answer ([headless](https://code.claude.com/docs/en/headless)).
Flags that matter for the engine (all present in `claude --help` of version 2.1.303):

- `--output-format text|json|stream-json`: `json` gives one result object, `stream-json` gives NDJSON events, the last of which is `result`.
- `--json-schema '<schema>'`: structured output, the result is in the field `structured_output`.
  An invalid schema gives `Error: --json-schema is not a valid JSON Schema`; the keyword `format` is not checked.
- `--model <alias|name>`, `--fallback-model`, `--effort low|medium|high|xhigh|max`: model and effort per call.
- `--session-id <uuid>`: the engine assigns the session ID itself in advance.
- `--resume <id|path.jsonl>`, `--continue`, `--fork-session`: continuing and forking a session.
- `--permission-mode acceptEdits|auto|bypassPermissions|manual|dontAsk|plan`, `--allowedTools`, `--disallowedTools`, `--tools`.
- `--permission-prompts none`: anything that would need a human answer is denied, and `AskUserQuestion` is removed from the tools (since v2.1.259).
- `--append-system-prompt`, `--system-prompt`, `--settings`, `--mcp-config`, `--strict-mcp-config`, `--agents`, `--plugin-dir`, `--add-dir`.
- `--max-budget-usd`: only for `-p`, based on the client-side cost estimate.
- `--no-session-persistence`: the session is not written to disk and cannot be continued.
- `--input-format stream-json`: a multi-turn dialog over stdin in one process.

Example of a Stage call:

```bash
claude -p "$PROMPT" \
  --session-id "$UUID" \
  --model sonnet --effort high \
  --output-format stream-json --verbose \
  --json-schema "$VERDICT_SCHEMA" \
  --permission-mode acceptEdits --permission-prompts none \
  --allowedTools "Bash(make *),Read,Edit"
```

### 1.2. Claude Agent SDK for Python

The package `claude-agent-sdk` ([quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart), [Python reference](https://code.claude.com/docs/en/agent-sdk/python)).
The SDK "runs the Claude Code binary" and bundles the native binary in the wheel ([overview](https://code.claude.com/docs/en/agent-sdk/overview), quickstart).
`cli_path` lets you substitute your own executable, and `transport` lets you replace the local subprocess with your own channel.
`query()` creates a new session per call, while `ClaudeSDKClient` holds a session and supports `interrupt()`.
`ClaudeAgentOptions` mirrors the CLI flags: `model`, `fallback_model`, `resume`, `continue_conversation`, `fork_session`, `session_id`, `output_format={"type": "json_schema", ...}`, `permission_mode`, `max_turns`, `max_budget_usd`, `setting_sources`, `env`.
On top of the CLI, the SDK gives Python callbacks: hooks, `can_use_tool`, `session_store` for continuing a session on another host.
Errors are typed: `CLINotFoundError`, `ProcessError(exit_code, stderr)`, `ResultError` (since SDK 0.2.140, carries `subtype`, `terminal_reason`, `api_error_status`, `session_id`).

On SDK authentication the documentation says this ([overview](https://code.claude.com/docs/en/agent-sdk/overview)):

> Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK.

This forbids a product for other people's users, not a personal script of the subscription owner (see section 4).
Technically the SDK inherits the CLI login, because it runs the same binary with the same `~/.claude`.

### 1.3. Interactive session in tmux or an Orca terminal

Claude Code supports background sessions: `claude --bg` prints an ID, `claude agents` lists them, `claude attach <id>`, `claude logs <id>`, `claude stop <id>` (from `claude --help`).
`claude -w <name> --tmux` creates a worktree and a tmux session for it.
`--remote-control` opens an interactive session controlled from claude.ai, but the `setup-token` token does not support it ([authentication](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)).
Only in an interactive session with a claude.ai login can Claude Code wait for a limit reset and continue the task on its own ([errors](https://code.claude.com/docs/en/errors#youve-hit-your-session-limit)).
The downside for the engine: no `result` event, no `structured_output`, no exit code per step, and input only through keyboard emulation (`tmux send-keys`).
Conclusion: Orca needs the interactive mode for watching and manual intervention, but a Gate must rely on the headless result.
A human can join the same session after a step through `claude --resume <session_id>` in an Orca terminal, since sessions are stored in `~/.claude/projects/<encoded-cwd>/*.jsonl` ([SDK sessions](https://code.claude.com/docs/en/agent-sdk/sessions)).

## 2. Structured output and exit codes

Claude Code (from [headless](https://code.claude.com/docs/en/headless)):

- Code 0 on success, non-zero on failure.
- Invalid flag: an error in stderr before start.
- A failure inside the run, such as missing authentication: printed as `result` to stdout, not to stderr.
- SIGTERM: code 143, the current turn stays unfinished and without a result; to finish the turn, send SIGINT or `interrupt()` first.
- The last `stream-json` event is `result` with the text, cost and session metadata.
- The first event is `system/init` with the model, tools, MCP servers, plugins and the `capabilities` array for feature detection.
- `system/api_retry` reports retries with the field `error` (`authentication_failed`, `rate_limit`, `billing_error`, `overloaded` and others).
- Permission denials show up as `permission_denied` and in `permission_denials` of the final result.

Result fields ([Python reference](https://code.claude.com/docs/en/agent-sdk/python)): `subtype` (`success`, `error_during_execution`, `error_max_turns`, `error_max_budget_usd`, `error_max_structured_output_retries`), `is_error`, `session_id`, `result`, `structured_output`, `terminal_reason`, `api_error_status`, `num_turns`, `duration_ms`, `usage`, `model_usage`, `total_cost_usd`.

Observation from a local run (2.1.303): with a wrong external key the CLI returned exit 1 and `{"subtype":"success","is_error":true,"api_error_status":401,"terminal_reason":"api_error","result":"Invalid API key · Fix external API key"}`.
So `subtype == "success"` does not mean success: the engine must look at `is_error`, `terminal_reason` and the process exit code together.
The same run showed that any `ANTHROPIC_API_KEY` or other external source in the environment overrides the subscription; in `-p` the key is always used, without asking ([authentication precedence](https://code.claude.com/docs/en/authentication#authentication-precedence)).
Consequence for the engine: explicitly remove `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_*` from the child process env.

## 3. Continuing and handing over a session

- `--resume <session_id>` finds a session by ID in any project on this machine; a path to a `.jsonl` can also be passed ([headless](https://code.claude.com/docs/en/headless#continue-conversations)).
- `--continue` takes the last session in the current directory; this is unreliable for a parallel engine, so use only an explicit ID.
- `--session-id <uuid>` lets you assign the ID before start, which makes recording the Run simpler.
- `--fork-session` (`fork_session=True`) forks the history, but not the file system ([SDK sessions](https://code.claude.com/docs/en/agent-sdk/sessions)).
- Session files are local to the machine; another host needs `session_store` in the SDK, and the store key is derived from `cwd`, so resume is done from the same path.
- For a Docker Sandbox this means: the directory `~/.claude/projects` (or `CLAUDE_CONFIG_DIR`) must live on a volume that outlives the container, and the Workspace path inside the container must be stable.
- A turn interrupted by SIGTERM is not continued on resume unless `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1` is set.
- `total_cost_usd` on resume counts the whole conversation, not the last call.

## 4. Subscription, authentication and terms of use

### 4.1. How to authenticate a non-interactive call

- By default the CLI and SDK take OAuth from `/login`, on macOS from the Keychain, on Linux from `~/.claude/.credentials.json` ([authentication](https://code.claude.com/docs/en/authentication#credential-management)).
- For containers and scripts: `claude setup-token` issues an OAuth token valid for a year, which goes into `CLAUDE_CODE_OAUTH_TOKEN`.
  Documentation: "For CI pipelines, scripts, or other environments where interactive browser login isn't available" and "This token authenticates with your Claude subscription and requires a Pro, Max, Team, or Enterprise plan".
  The token can only make model requests: no Remote Control and no claude.ai connectors.
- `--bare` never reads OAuth or `CLAUDE_CODE_OAUTH_TOKEN`, only `ANTHROPIC_API_KEY` or `apiKeyHelper` ([headless](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode)).
  For an owner without an API key, `--bare` is not available.
  Risk: the documentation promises that `--bare` "will become the default for `-p` in a future release"; the engine will need to turn bare off explicitly or pass context through flags, and to watch the changelog.
- Without `--bare`, a `-p` call picks up hooks, skills, MCP and CLAUDE.md from `~/.claude` and from the project, and without a trust dialog.
  For reproducibility the engine should set `--setting-sources`, `--strict-mcp-config` and a separate `CLAUDE_CONFIG_DIR` per Sandbox.

### 4.2. What the terms say

- Pro and Max fall under the [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) ([legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)).
- Consumer Terms (as of October 8, 2025), section 3: it is forbidden "to access the Services through automated or non-human means, whether through a bot, script, or otherwise", except for access "via an Anthropic API Key or where we otherwise explicitly permit it".
- The explicit permission for Claude Code is in the official documentation: `-p`, the Agent SDK and `setup-token` for "CI pipelines, scripts" are described as working with a subscription.
- [Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance): "Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK."
- In the same place: OAuth "is designed to support ordinary use of Claude Code and other native Anthropic applications"; product developers "should use API key authentication"; it is forbidden to "offer Claude.ai login into their own applications, or to route requests through Free, Pro, or Max plan credentials on behalf of their users" and to "collect, store, or intermediate Claude.ai credentials or session tokens".
- Allowed in the same place: "Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription".
- Anthropic reserves the right to take action "without prior notice".
- The account cannot be passed to others (Consumer Terms, section 2).

Interpretation for the owner's factory:
an engine that, on the owner's machine, runs an unmodified `claude` with the owner's login for the owner's own projects stays within permitted use.
The gray area is volume: a factory with many parallel Runs is already far from "ordinary, individual usage", and no sanction is formally described, but the "without prior notice" rule exists.
What the engine must not do: pull the OAuth token out of the Keychain and call the API directly, patch the binary, or give other people access to the factory on the owner's subscription.
If the factory becomes shared or a product, it needs an API key or Team/Enterprise and the Commercial Terms.

## 5. Subscription limits and parallel sessions

- Pro and Max have a five-hour window and a weekly limit, shared between claude.ai and Claude Code ([support](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan)).
- Limits are counted per account, not per session: parallel sessions draw from one pool.
  Primary sources give no explicit number of allowed parallel sessions.
- Messages: `You've hit your session limit · resets 3:45pm`, `You've hit your weekly limit`, `You've hit your Opus limit`, `You've hit your Sonnet limit` ([errors](https://code.claude.com/docs/en/errors#youve-hit-your-session-limit)).
  Session and weekly limits are shared across all models; Opus and Sonnet limits are separate, and switching to the other family helps.
- "A single burst of heavy activity, such as a large workflow fanout, can exhaust the weekly allowance before the session window resets."
- Automatic waiting for the reset exists only in an interactive session; for `-p` the engine must catch the refusal itself and schedule a retry after `resets_at`.
- The SDK emits `RateLimitEvent` with `RateLimitInfo`: `status` (`allowed`, `allowed_warning`, `rejected`), `rate_limit_type` (`five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet`, `overage`), `utilization` 0..1, `resets_at` ([Python reference](https://code.claude.com/docs/en/agent-sdk/python#ratelimitevent)).
  This is a ready-made signal for Orchestrator backpressure: lower parallelism on `allowed_warning`, pause Tickets on `rejected`.
- The fallback model (`--fallback-model`) does not kick in on rate-limit and billing errors ([model config](https://code.claude.com/docs/en/model-config)).
- Retries of transient errors: up to 10 with exponential backoff; `CLAUDE_CODE_RETRY_WATCHDOG=1` retries 429/529 forever, but not an exhausted quota.
- Beyond the limit you can turn on usage credits at API rates (`/usage-credits`); on Max, the built-in API credits do not work in Claude Code.
- Depending on the plan, Fable may be charged to usage credits and require consent; in an SDK session with no answer, the turn ends with the message "the prompt to confirm went unanswered" ([errors](https://code.claude.com/docs/en/errors)).
  The engine should not pick Fable by default, or should settle the usage credits question explicitly.

## 6. Choosing the model per call

- Order: `/model` in the session, `--model`, `ANTHROPIC_MODEL`, `model` in settings, `ANTHROPIC_DEFAULT_MODEL` ([model config](https://code.claude.com/docs/en/model-config)).
- Aliases: `default`, `best`, `fable`, `opus`, `sonnet`, `haiku`, `opus[1m]`, `sonnet[1m]`, `opusplan`.
- Default for Pro, Max, Team, Enterprise and API: Opus 5.5.
- `--effort` picks the level per call; for Opus 5.5 and Sonnet 5.5 the default is `medium`.
- 1M context on Pro requires usage credits; on Max it is on for Opus 4.6.
- In `-p` you can pass `/model sonnet` in the prompt, but for the engine the right way is the `--model` flag on every Stage call.

## 7. Codex CLI as a second executor

Sources: [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode), [CLI reference](https://learn.chatgpt.com/docs/cli/reference), [auth](https://learn.chatgpt.com/docs/auth), [pricing](https://learn.chatgpt.com/docs/pricing).

- `codex exec "<task>"`: progress to stderr, the final message to stdout; `codex exec -` reads the prompt from stdin.
- `--json`: JSONL events `thread.started`, `turn.started`, `turn.completed`, `turn.failed`, `item.*`, `error`.
- `--output-schema <file>`: the final answer follows a JSON Schema; `-o/--output-last-message <path>` writes it to a file.
- `codex exec resume <SESSION_ID>` or `--last` (by the current directory, `--all` across all).
- `-m/--model` per call, `-p/--profile`, `-c key=value` for everything else (for example reasoning effort through config), `--ephemeral` without recording the session.
- Sandbox: `read-only` by default, `--sandbox workspace-write`, `danger-full-access`; `--full-auto` is no longer supported, and `--approve-for-me` or `--dangerously-bypass-approvals-and-sandbox` replace it.
- Requires a git repository, otherwise `--skip-git-repo-check`.
- The exit codes of `codex exec` are not documented; the Stage status has to come from `turn.completed`/`turn.failed`/`error` in the JSONL.
- Authentication: by default `codex exec` uses the saved CLI login, that is, the ChatGPT subscription.
  For headless: device code login or a copy of `~/.codex/auth.json` ("Treat ~/.codex/auth.json like a password"); Codex refreshes the tokens itself.
  OpenAI calls ChatGPT authentication in CI an "advanced" option for trusted runners, "API keys are the right default for automation", "Do not use this workflow for public or open-source repositories".
  `codex exec`, the Codex SDK and scripted scenarios are listed as available on Plus.
- Limits: local messages and cloud tasks share one plan pool; Plus has a five-hour window, "Pro plans currently have no five-hour limit", weekly limits "may also apply"; beyond the limit you buy credits.
  There is no number for parallel tasks.
- The OpenAI Terms of Use were not read in this research (the page returned 403); the conclusion relies only on the Codex documentation.

## 8. Recommendation for the Spec

1. A single executor contract: a CLI subprocess with NDJSON events and a JSON Schema for the verdict.
   Claude: `claude -p --output-format stream-json --verbose --json-schema ...`; Codex: `codex exec --json --output-schema ...`.
   This works the same on the host and across the Docker Sandbox boundary (`docker exec ... claude -p ...`).
2. Use the Python Agent SDK as the implementation of the Claude adapter if callbacks are needed (`can_use_tool`, hooks, typed `RateLimitEvent`); through `cli_path` pointing at a `docker exec` wrapper it also crosses the Sandbox boundary.
   Authentication and limits do not depend on the choice.
3. Interactive mode (tmux, Orca, `claude --bg`/`attach`) only for watching and manual takeover, continuing the session by `session_id` through `--resume`.
4. Authentication in the Sandbox: `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token` for Claude, `auth.json` or device code for Codex; no `--bare`; explicitly clean API keys and provider variables out of the child env.
5. Sessions: the engine assigns `--session-id`, stores it in the Run, and continues only through an explicit `--resume`; a volume for `CLAUDE_CONFIG_DIR` and a stable Workspace path in the container.
6. Stage status: the process exit code plus `is_error`, `subtype`, `terminal_reason`, `api_error_status`; `subtype == "success"` on its own is not success.
7. Limits: a global per-account parallelism semaphore in the Orchestrator, backpressure on `RateLimitEvent`/`api_retry`, pausing Tickets until `resets_at` rather than failing the Run.
8. Model and effort are set in the Pipeline per Stage and passed through the flags `--model`/`--effort` and `-m`/`-c`.
9. Terms: the factory is for the owner only, on unmodified binaries, without extracting tokens; when extending to other people, switch to an API key or Team/Enterprise.

## Open questions

- The exact behavior of `-p` when the quota runs out (exit code, `terminal_reason`, whether `resets_at` is in the CLI result without the SDK) was not reproduced, since it requires actually hitting the limit.
- How many parallel sessions Max sustains without refusals: primary sources give no number, so it needs a measurement in a prototype issue.
- How `-p` authentication will change once `--bare` becomes the default.
- The exit codes of `codex exec` need to be checked live.
