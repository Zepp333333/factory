# How the factory would run OpenAI Codex CLI as a second Executor

Research date: 2026-11-03.
Codex is not installed on this machine, so nothing here was run.
Sources are the openai/codex repository at commit `f05aa41da6d0` (2026-11-03, cloned shallow from https://github.com/openai/codex) and the Codex docs, which `developers.openai.com/codex/*` now redirects to `learn.chatgpt.com/docs/*` (HTTP 308).
Repo paths below are relative to the repository root, so `codex-rs/exec/src/lib.rs` means https://github.com/openai/codex/blob/main/codex-rs/exec/src/lib.rs.
The workspace `Cargo.toml` and `codex-cli/package.json` carry placeholder versions (`0.0.0`), so no release number could be pinned; a released binary may lag `main`.
Anything marked **unverified** comes from neither the source nor the docs, or needs a live run.
Companion docs: [claude-code-subscription-exec.md](claude-code-subscription-exec.md) for the Claude side, and the port in `src/factory/ports/executor.py`.

## Short answer

`codex exec --json` is the Codex counterpart of `claude -p --output-format stream-json`: a child process that prints JSONL events to stdout and ends after one turn.
It covers the headless run, structured output (`--output-schema`, sent to the model as strict Structured Outputs), resume by thread ID, model and effort per call, and an isolated config through `CODEX_HOME`.
It is thinner than Claude's stream in four ways that matter for the port:
there is no init event that lists loaded skills and MCP servers,
there is no cost in USD,
rate-limit windows are not in the `exec --json` stream at all (they are in the session rollout file and in the `app-server` protocol),
and errors carry only a message string, while the typed error class (`codexErrorInfo`) is dropped by `exec`.
The richer channel is `codex app-server --listen stdio://`, a JSON-RPC protocol that the official Python SDK uses; it has typed errors, `account/rateLimits/updated` and per-turn token usage with the context window.
The factory has to choose between `exec --json` plus reading the rollout file, and a JSON-RPC client for `app-server`.

## 1. Headless run

### 1.1. Flags

From `codex-rs/exec/src/cli.rs` and `codex-rs/utils/cli/src/shared_options.rs` (`SharedCliOptions`):

- `codex exec [OPTIONS] [PROMPT]`; subcommands `resume`, `fork`, `review`.
- `--json` (alias `--experimental-json`): JSONL events on stdout.
- `-o/--output-last-message FILE`: writes the final agent message to a file.
- `--output-schema FILE`: JSON Schema for the final message (section 3).
- `-m/--model`, `-p/--profile NAME`, `-c/--config key=value` (repeatable, TOML value).
- `-s/--sandbox read-only|workspace-write|danger-full-access`.
- `--dangerously-bypass-approvals-and-sandbox` (alias `--yolo`): sets `danger-full-access` and skips the Git check, "Intended solely for running in environments that are externally sandboxed" (`shared_options.rs`, `exec/src/lib.rs` near "When --yolo").
- `--approve-for-me` (alias `--not-so-yolo`): approvals go to an automatic reviewer under `workspace-write`; conflicts with `--sandbox` and `--yolo`.
- `--dangerously-bypass-hook-trust`: runs enabled hooks without persisted hook trust.
- `-C/--cd DIR`: working root; `--add-dir DIR`: more writable roots; `--worktree`: run in a new managed Git worktree.
- `--skip-git-repo-check`: without it `exec` refuses to run outside a Git repo with "Not inside a trusted directory and --skip-git-repo-check was not specified." and exit 1 (`exec/src/lib.rs`).
- `--ephemeral`: no session rollout files on disk.
- `--ignore-user-config`: does not load `$CODEX_HOME/config.toml`, "auth still uses `CODEX_HOME`".
- `--ignore-rules`: skips user and project execpolicy `.rules` files.
- `--strict-config`: errors on config fields this version does not know.
- `-i/--image`, `--oss`/`--local-provider`, `--thread-source`, `--cyber-access-program`, `--color`.
- There is no flag to assign the session ID in advance, unlike Claude's `--session-id`; the ID arrives in the first event, `thread.started` (section 2).
- There is no `--max-turns` or budget flag (searched `cli.rs` and `shared_options.rs`).

### 1.2. Prompt from a file or stdin

The `PROMPT` doc comment in `cli.rs`: "If not provided as an argument (or if `-` is used), instructions are read from stdin. If stdin is piped and a prompt is also provided, stdin is appended as a `<stdin>` block."
So `codex exec - < prompt.md` reads the whole prompt from stdin, and an argv prompt works the same way the Claude adapter passes it today.
There is no `--prompt-file` flag.

### 1.3. Approvals and sandbox

- `exec` forces `approval_policy = never` unless an auto reviewer is configured: "Default to never ask for approvals in headless mode" (`exec/src/lib.rs`, `ConfigOverrides { approval_policy: Some(AskForApproval::Never) ... }`).
- If an approval request still reaches `exec` (for example under `--approve-for-me` without a reviewer), it is refused with "... approval is not supported in exec mode" and marks the run failed (`handle_server_request` in `exec/src/lib.rs`).
- The default sandbox is `read-only`; the docs say "`--full-auto` is no longer supported" and to use `--sandbox workspace-write` ([non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)).
- Inside the factory's own Sandbox the right setting is `--dangerously-bypass-approvals-and-sandbox`: it is the documented external-sandbox mode, and Codex's own Linux sandbox (bubblewrap/Landlock/seccomp, `codex-rs/linux-sandbox`, `codex-rs/bwrap`) may not work inside a container anyway (**unverified** in our Docker Sandbox).

### 1.4. Resume and fork

- `codex exec resume <SESSION_ID|thread name> [PROMPT]` or `resume --last [PROMPT]` (`--all` disables the cwd filter) (`cli.rs` `ResumeArgs`).
- `codex exec fork <SESSION_ID> [PROMPT]` forks a session into a new one; output options require a prompt (`exec/src/lib.rs`: "Forking with output options requires a prompt").
- The session ID is the `thread_id` of `thread.started`: "Can be used to resume the thread later" (`exec/src/exec_events.rs`).
- Sessions live as rollout files under `$CODEX_HOME/sessions/YYYY/...` named `rollout-<timestamp>-<thread_id>.jsonl` (`codex-rs/rollout/src/recorder.rs`), so resume needs the same `CODEX_HOME`; `--ephemeral` makes a session impossible to resume.

## 2. The `exec --json` stream

### 2.1. Event types and shapes

The full schema is `codex-rs/exec/src/exec_events.rs`; the mapping from internal notifications is `codex-rs/exec/src/event_processor_with_jsonl_output.rs`.
Top-level events, tagged by `type`:

| `type` | Payload |
| --- | --- |
| `thread.started` | `{"thread_id": "<uuid>"}` |
| `turn.started` | `{}` |
| `turn.completed` | `{"usage": {"input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens"}}` |
| `turn.failed` | `{"error": {"message": "..."}}` |
| `item.started`, `item.updated`, `item.completed` | `{"item": {"id": "item_N", "type": "...", ...}}` |
| `error` | `{"message": "..."}` |

Item types (`ThreadItemDetails`, snake_case `type`):

| Item `type` | Fields | When |
| --- | --- | --- |
| `agent_message` | `text` (a JSON string under `--output-schema`) | `item.completed` only |
| `reasoning` | `text` (the reasoning summary; items with an empty summary are dropped) | `item.completed` only |
| `command_execution` | `command`, `aggregated_output`, `exit_code`, `status`: `in_progress`/`completed`/`failed`/`declined` | started at spawn, completed at exit |
| `file_change` | `changes: [{path, kind: add/delete/update}]`, `status`: `in_progress`/`completed`/`failed` | completed only, per the doc comment |
| `mcp_tool_call` | `server`, `tool`, `arguments`, `result: {content, structured_content, _meta}`, `error: {message}`, `status` | started and completed |
| `collab_tool_call` | `tool`: `spawn_agent`/`send_input`/`wait`/`close_agent`, `sender_thread_id`, `receiver_thread_ids`, `prompt`, `agents_states`, `status` | subagent calls |
| `web_search` | `id`, `query`, `action`, `results` | started and completed |
| `todo_list` | `items: [{text, completed}]` | started, updated, completed at turn end |
| `error` | `message` | non-fatal: config warnings, deprecation notices, "model rerouted: A -> B", lagged-event warnings |

Docs example lines ([non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)):
`{"type":"thread.started","thread_id":"0199a213-81c0-7800-8aa1-bbab2a035a53"}`,
`{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"bash -lc ls","status":"in_progress"}}`,
`{"type":"turn.completed","usage":{"input_tokens":24763,"cached_input_tokens":24448,"output_tokens":122,"reasoning_output_tokens":0}}`.

What the stream does not carry:

- No init event: `thread.started` has only `thread_id`, with no model, tools, skills, MCP servers or plugins (`ThreadStartedEvent`).
- No streaming text deltas: `agent_message` and `reasoning` appear only once complete (`map_started_item` skips them).
- No command output while a command runs: `aggregated_output` comes with `item.completed`.
- No rate-limit events: `account/rateLimits/updated` falls into the `_ => false` arm of `should_process_notification` in `exec/src/lib.rs` and is never printed.
- No events of subagent threads: only notifications whose `thread_id` and `turn_id` match the primary turn pass the same filter; a subagent shows only as `collab_tool_call` items of the main thread.
- No model name, duration or context window.

### 2.2. Which events end the run

`turn.completed` and `turn.failed` end the stream: both return `CodexStatus::InitiateShutdown`, and the process exits after the in-process server shuts down (`collect_thread_events`).
An interrupted turn (SIGINT routes to `turn/interrupt`) emits neither and still ends the run with exit 1 (`TurnStatus::Interrupted` arm and `error_seen` in `run_exec_session`).
An `error` event does not end the run by itself: `exec` prints every `Error` notification of the turn, including transient ones with `will_retry = true` (stream reconnects; defaults are 5 stream retries and 4 request retries, `codex-rs/model-provider-info/src/lib.rs`), but the JSON drops `will_retry`.
So the adapter must treat `error` as information and decide only on `turn.completed`, `turn.failed`, and the exit code.

### 2.3. Progress for stall detection

- Progress: `item.started`, `item.updated`, `item.completed` and `turn.*`.
- There is no heartbeat event at all, so unlike Claude there is nothing to exclude.
- A hanging command shows as `item.started` of a `command_execution` and then silence until it exits, and a long model call shows as silence until the `agent_message` or `reasoning` item completes.
  The stall timer must therefore cover the longest model response without deltas; the provider's own stream idle timeout is 300 s (`DEFAULT_STREAM_IDLE_TIMEOUT_MS`, `model-provider-info/src/lib.rs`).
- Non-fatal `error` items (warnings) arrive without the agent doing anything, so `is_progress` should not count `item.completed` of type `error`.

## 3. Structured output

- `--output-schema FILE` reads a JSON file; a missing or non-JSON file exits 1 before the run with "Failed to read output schema file ..." or "Output schema file ... is not valid JSON" on stderr (`load_output_schema` in `exec/src/lib.rs`).
- The schema goes to the Responses API as `text.format = {type: "json_schema", name: "codex_output_schema", strict: true, schema}` (`create_text_param_for_request` in `codex-rs/codex-api/src/common.rs`; `output_schema_strict: true` in `codex-rs/core/src/client_common.rs`).
  So the schema must fit OpenAI's strict Structured Outputs subset: every property in `required`, `additionalProperties: false` on every object, and only the supported keywords.
  The docs' example schema follows exactly that pattern ([non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)); the subset's exact keyword list is in the API docs, not in Codex (**unverified** which keywords the factory's current schemas use that strict mode rejects).
- Codex does no client-side validation of the schema or of the answer that I could find in `exec`; an unsupported schema is rejected by the API, which would surface as `turn.failed` with the API's message (**unverified** wording).
- The answer is the `text` of the last `agent_message`, a JSON string the adapter must parse; there is no `structured_output` field.
- `-o FILE` is written only when the turn completed (`emit_final_message_on_shutdown`), with the last `agent_message` text, or an empty file plus a stderr warning when there was none (`handle_last_message` in `exec/src/event_processor.rs`).
  The file handle is bound before the agent starts, so "Sandbox activity cannot redirect the final write" (`exec/src/lib.rs`).
- A model refusal under strict mode (Responses API `refusal` content) is not mapped in `exec_events.rs`; how it shows is **unverified**.

## 4. Usage and cost

- `turn.completed.usage` has `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens` (`exec_events.rs` `Usage`).
- Despite the doc comment "used during the turn", the values are the thread's running total: `usage_from_last_total` copies `token_usage.total` (`event_processor_with_jsonl_output.rs`).
  For a fresh `exec` run that is the same thing; after `exec resume` it may include earlier turns (**unverified** whether the total is restored from the rollout on resume).
- `input_tokens` includes the cached ones: core computes `non_cached_input = input_tokens - cached_input_tokens` (`TokenUsage::non_cached_input` in `codex-rs/protocol/src/protocol.rs`).
- Subagent threads' tokens are not in the main thread's usage (section 2.1 filter).
- There is no cost in USD anywhere in the protocol (searched for `cost_usd`/`usd`: only backend rate-limit models and unrelated crates).
  Cost must be computed from a price table per model, and on a ChatGPT plan it is only an API price equivalent, as for Claude.
- No `num_turns`; an `exec` run is one user turn, while the count of model requests inside it is not reported.
- `model_context_window` and the last request's usage exist in `ThreadTokenUsage {total, last, model_context_window}` (`codex-rs/app-server-protocol/src/protocol/v2/thread.rs`) but `exec --json` drops them; they are in the rollout file's `token_count` events (section 5).

## 5. Quotas

- Rate limits come from response headers of the `x-codex-*` family: `x-codex-primary-used-percent`, `x-codex-primary-window-minutes`, `x-codex-primary-reset-at`, the same for `secondary`, plus other limit IDs such as `x-codex-other-*` (`codex-rs/codex-api/src/rate_limits.rs`).
- The snapshot is `RateLimitSnapshot {limit_id, limit_name, primary, secondary, credits, individual_limit, spend_control_reached, plan_type, rate_limit_reached_type}` with `RateLimitWindow {used_percent (0-100), window_minutes, resets_at (Unix seconds)}` (`codex-rs/protocol/src/protocol.rs`).
- Core emits it in `token_count` events: `TokenCountEvent {info: TokenUsageInfo, rate_limits: RateLimitSnapshot}`.
- `exec --json` does not print it (section 2.1).
  It is persisted in the rollout file: `persisted_event_msg` keeps `EventMsg::TokenCount` (`codex-rs/rollout/src/policy.rs`), so the adapter can tail `$CODEX_HOME/sessions/.../rollout-*-<thread_id>.jsonl` for `token_count` lines, unless the run is `--ephemeral`.
  The exact JSON line shape in the rollout file is **unverified** without a live run.
- `codex app-server` streams it as `account/rateLimits/updated` and answers `account/rateLimits/read` (`codex-rs/app-server-protocol/src/protocol/common.rs`).
- Plans: Plus has a five-hour window, "Pro plans currently have no five-hour limit", "Weekly limits may also apply", local and cloud tasks share one allowance, and over the limit Plus/Pro can buy credits ([pricing](https://learn.chatgpt.com/docs/pricing)).
  Which of `primary`/`secondary` is the five-hour and which the weekly window is not fixed in code; the adapter must key windows by `window_minutes` (300 and 10080 are the expected values, **unverified**).
- API-key billing: whether `api.openai.com` sends the `x-codex-*` headers is **unverified**; the API has its own `x-ratelimit-*` headers that Codex does not parse, so expect no windows under an API key.
- Hitting the limit: `UsageLimitReached` with text such as "You’ve hit your usage limit. ... Try again at <time>." (`UsageLimitReachedError` Display in `codex-rs/protocol/src/error.rs`), classified `UsageLimitExceeded`; `QuotaExceeded` ("Quota exceeded. Check your plan and billing details.") is the API-key variant.
  In `exec --json` only the message string reaches `turn.failed`, so `resets_at` must be parsed from text or taken from the last rollout `token_count`.

## 6. Authentication

- Two methods: "Sign in with ChatGPT for subscription access" and "Sign in with an API key for usage-based access", API keys billed "at standard API rates" ([auth](https://learn.chatgpt.com/docs/auth)).
- Credential store `cli_auth_credentials_store`: `file` (`$CODEX_HOME/auth.json`), `keyring`, `auto`, `ephemeral` ([auth](https://learn.chatgpt.com/docs/auth)).
  A Sandbox needs `file`, since a container has no OS keyring.
- Precedence in `load_auth` (`codex-rs/login/src/auth/manager.rs`): `CODEX_API_KEY` first ("API key via env var takes precedence over any other auth method", enabled for `exec` at `enable_codex_api_key_env: true` in `exec/src/lib.rs`), then in-memory external ChatGPT tokens, then `CODEX_ACCESS_TOKEN` (personal access token or agent identity JWT, Enterprise automation), then the stored `auth.json`.
- `OPENAI_API_KEY` is not read by `exec` for model calls; it is used by `codex login --with-api-key` from stdin, the TUI prefill and realtime voice (`read_openai_api_key_from_env` call sites).
  `auth.json` itself has an `OPENAI_API_KEY` field for a stored key (`login/src/auth/storage.rs`).
- So, as with Claude, the adapter must scrub `CODEX_API_KEY` and `CODEX_ACCESS_TOKEN` from the child env to keep the run on the subscription.
- ChatGPT tokens refresh automatically through `https://auth.openai.com/oauth/token` (`REFRESH_TOKEN_URL`), and refresh tokens are single use: "Your access token could not be refreshed because your refresh token was already used. Please log out and sign in again." (`REFRESH_TOKEN_REUSED_MESSAGE`).
  Copying one `auth.json` into several parallel `CODEX_HOME`s therefore risks logging every copy out after the first refresh; the safe shapes are a proxy that owns the token on the host (as the Sandbox proxy does for Claude) or one shared `CODEX_HOME` for auth (**unverified** how the factory's `sbx` proxy would handle ChatGPT's `Authorization` and account headers).
- Headless login: `codex login --device-auth` (beta, must be enabled in ChatGPT security settings), or copy `~/.codex/auth.json`, which the docs say to treat "like a password" ([auth](https://learn.chatgpt.com/docs/auth)).
- `forced_login_method = "chatgpt"|"api"` restricts the method; a mismatch logs out and exits ([auth](https://learn.chatgpt.com/docs/auth)); in `exec` this check prints the error and exits 1 (`enforce_login_restrictions` in `exec/src/lib.rs`).
- Hosts for an egress allowlist, from source constants:
  - `chatgpt.com`: model traffic under ChatGPT auth (`CHATGPT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"`, `model-provider-info/src/lib.rs`), the models catalog, and analytics (`/backend-api/codex/analytics-events/events`, `codex-rs/analytics`).
  - `api.openai.com`: model traffic under an API key (`https://api.openai.com/v1/responses`).
  - `auth.openai.com`: token refresh and login.
  - Whatever MCP servers the kit configures.
  - Analytics is on by default in `exec` (`DEFAULT_ANALYTICS_ENABLED = true`) and can be turned off with `analytics.enabled = false` ([config reference](https://learn.chatgpt.com/docs/config-file/config-reference)).
  - Whether `exec` also checks for updates or calls other hosts is **unverified**; `check_for_update_on_startup` exists in config.
- Env vars `exec` reads that matter: `CODEX_HOME`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, `CODEX_CA_CERTIFICATE` (falls back to `SSL_CERT_FILE`), `CODEX_SQLITE_HOME`, `HOME` (for `~/.agents/skills`, section 8), `RUST_LOG` (**unverified** for `exec`; it sets its own filter `EXEC_DEFAULT_LOG_FILTER`).
- `CODEX_HOME` must exist and be a directory, or Codex fails with "CODEX_HOME points to ..., but that path does not exist" (`codex-rs/utils/home-dir/src/lib.rs`).

## 7. Models and effort

- `-m/--model <slug>` or `model = "..."` in config.
- The catalog is fetched from the backend's `/models` endpoint with a 300 s cache (`DEFAULT_MODEL_CACHE_TTL`, `codex-rs/models-manager/src/manager.rs`), with a bundled fallback in `codex-rs/models-manager/models.json`.
  Listed slugs in the bundled file: `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-6.1-terra`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5` (in priority order; hidden ones such as `codex-auto-review` omitted).
  The config reference uses `gpt-6.1-sol` as its example ([config reference](https://learn.chatgpt.com/docs/config-file/config-reference)).
- The default model is the catalog entry marked default, else the first by priority (`default_model_from_available`), so without `-m` it changes with the remote catalog; the adapter should always pass `-m`.
- A model the account cannot use is `ModelNotFound` at request time; the docs say the model "must be available to your signed-in account or workspace".
- Effort: `-c model_reasoning_effort="<level>"`; the enum is `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`, `persistent` plus unknown model-defined values (`ReasoningEffort` in `codex-rs/protocol/src/openai_models.rs`).
  Each model supports a subset: the bundled catalog gives `low` to `ultra` for most `gpt-6*`, `low` to `max` for the `luna` models and `low` to `xhigh` for `gpt-5.5` (`supported_reasoning_levels` in `models.json`).
  So the effort check must be per model, which the port's `settings_problem(model, effort)` expresses since #266.
- Reasoning summaries, which become `reasoning` items, follow `model_reasoning_summary = auto|concise|detailed|none`.

## 8. Kit and agent config

Claude's kit maps onto Codex like this:

| Claude | Codex | Source |
| --- | --- | --- |
| `CLAUDE.md` / `--append-system-prompt-file` | `AGENTS.md` from project root to cwd, `AGENTS.override.md`, `$CODEX_HOME/AGENTS.md` as user instructions; `developer_instructions = "..."` in config; `model_instructions_file` replaces the built-in instructions | `codex-rs/core/src/agents_md.rs`, [config reference](https://learn.chatgpt.com/docs/config-file/config-reference) |
| Skills in a plugin | `skills/<name>/SKILL.md`; roots: `$CODEX_HOME/skills` (deprecated user location), `$HOME/.agents/skills`, `$CODEX_HOME/skills/.system` (bundled), `/etc/codex/skills` (admin), `.codex/skills` and `.agents/skills` in the repo from project root to cwd, plus plugin skills | `codex-rs/ext/skills/src/host_roots.rs`, `codex-rs/skills/src/lib.rs` |
| Subagents `agents/*.md` | Agent roles as `agents/*.toml` in each config folder (`$CODEX_HOME/agents`, `.codex/agents`), or `[agents.<name>] description, config_file`; multi-agent tools on by default (`features.multi_agent`, `agents.enabled`) | `codex-rs/agent-roles/src/discovery.rs`, `loader.rs`, config reference |
| `--mcp-config` + `--strict-mcp-config` | `[mcp_servers.<id>]` with `command`, `args`, `env`, `url`, `enabled`, `required`, `enabled_tools`, timeouts; `required = true` makes `exec` exit on a failed server | config reference, [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode) |
| `--plugin-dir` | Plugins keyed `name@marketplace`, enabled by `plugins.<id>.enabled`; manifests `plugin.json` (Agent Plugins spec), `.codex-plugin/plugin.json`, and `.claude-plugin` marketplaces are read too; no per-run plugin-dir flag found | `codex-rs/core-plugins/src/marketplace.rs`, `codex-rs/utils/plugins/src/plugin_namespace.rs` |
| `--settings` | `config.toml` layers, `-c key=value`, `-p NAME` = `$CODEX_HOME/NAME.config.toml` on top of the user config | `shared_options.rs`, config reference |
| `--setting-sources ""` | Project `.codex/config.toml`, hooks and exec policies load only for a project marked trusted in the user config; `--ignore-user-config`, `--ignore-rules` | `codex-rs/config/src/loader/mod.rs` (`disabled_reason_for_decision`) |

Skill toggles: `[[skills.config]]` entries with `name` or `path` and `enabled` ([config reference](https://learn.chatgpt.com/docs/config-file/config-reference)).
The skill file format (front matter fields) is in `codex-rs/skills/src/parser.rs`; the docs page `learn.chatgpt.com/docs/skills` returned 404, so field-level details are **unverified** here.

Isolated, fully specified config for one run:

1. `CODEX_HOME=<agent_home>` holding the factory's `config.toml`, `auth.json` (or the proxy setup), `AGENTS.md`, `skills/` and `agents/`; this replaces Claude's `CLAUDE_CONFIG_DIR` and `assemble` would write skills into `<agent_home>/skills`.
2. `HOME=<an empty dir>`, because `$HOME/.agents/skills` is read from the real home dir, not from `CODEX_HOME` (`resolve_skill_roots` uses `home_dir()`).
3. `--strict-config` so a typo fails the run instead of being ignored.
4. No `/etc/codex/config.toml`, `/etc/codex/requirements.toml`, `/etc/codex/managed_config.toml` or `/etc/codex/skills` in the Sandbox image (`codex-rs/config/src/loader/mod.rs`, `layer_io.rs`).
5. Under `profile: hermetic`, leave the Workspace untrusted so its `.codex/config.toml` and hooks stay off; repo `AGENTS.md` and `.agents/skills` still load unless the project is explicitly marked `untrusted` (`load_project_instructions` returns early only on `is_untrusted()`), so a hermetic run needs `[projects."<workspace>"] trust_level = "untrusted"`, which in turn also drops `AGENTS.md` (**unverified** whether it also drops repo skills).
6. `analytics.enabled = false`, `history.persistence = "none"`, `features.memories = false` (default off), `check_for_update_on_startup = false`.

There is no init event to verify what loaded (section 2.1), so the `agent_config` check of the Claude adapter has no direct Codex equivalent in `exec`.
The `app-server` protocol has skills and MCP status requests (**unverified** which ones fit a pre-flight check).

## 9. Exit codes and failure classes

- 0: the turn completed.
- 1: everything else that `exec` handles: a non-retried `error` of the turn, a `turn.failed`, an interrupted turn, a refused approval request, and every startup failure (bad `-c`, config load error, rules error, login restriction, missing Git repo, unreadable schema, missing `CODEX_HOME`) (`std::process::exit(1)` sites and `error_seen` in `exec/src/lib.rs`).
- 2: a CLI usage error, as clap's default (**unverified** that Codex keeps clap's default).
- SIGTERM: no handler found in `exec`; the exit status and whether the rollout is flushed are **unverified**.
  SIGINT goes through `turn/interrupt` and ends with exit 1.
- Startup failures print to stderr and emit no JSON; failures inside the turn print `turn.failed` on stdout.
- Typed classes exist (`CodexErrorInfo` in `codex-rs/protocol/src/protocol.rs`, mapping in `to_codex_protocol_error` in `protocol/src/error.rs`), but `exec --json` keeps only `message` (plus `additional_details` in parentheses), so with `exec` the adapter classifies by text:

| Failure | `CodexErrorInfo` | Message in `turn.failed` |
| --- | --- | --- |
| Auth | `Unauthorized` (refresh failures); a 401 shows as `HttpConnectionFailed{401}` | "Your access token could not be refreshed ... Please log out and sign in again." variants; 401 text **unverified** |
| Usage limit (plan) | `UsageLimitExceeded` | "You’ve hit your usage limit. ... Try again at ..." |
| Quota (API key) | `UsageLimitExceeded` | "Quota exceeded. Check your plan and billing details." |
| Plan without Codex | `UsageLimitExceeded` | "To use Codex with your ChatGPT plan, upgrade to Plus: ..." |
| Transient rate limit | `RateLimitExceeded` | "rate limit exceeded: ..." (retried first) |
| Context overflow | `ContextWindowExceeded` | "Codex ran out of room in the model's context window. ..." |
| Overloaded | `ServerOverloaded` | "Selected model is at capacity. Please try a different model." |
| Retries exhausted | `ResponseTooManyFailedAttempts{status}` | `RetryLimit` text |
| Unknown model | `ModelNotFound` | server message |
| Policy refusal | `CyberPolicy`, `BioPolicy`, `MisalignmentPolicyViolation` | server message; a plain model refusal in text is not an error |
| Content filter | none specific | "stream disconnected before completion: Incomplete response returned, reason: content_filter" |
| Sandbox | `SandboxError` | "sandbox error: ..." |

- Auto-compaction exists, so context overflow should be rare inside one turn (`compact_prompt`, `codex-rs/core/src/compact_remote_v2_attempt.rs`); when it happens the run fails as above.
- `app-server` keeps the typed `codexErrorInfo` and `willRetry` in its `error` notification (`ErrorNotification` in `app-server-protocol/src/protocol/v2/notification.rs`), which is the strongest reason to consider it over `exec`.

## 10. `exec --json` or `app-server`

- `exec --json` is what the TypeScript SDK wraps (`sdk/typescript/src/exec.ts` spawns `codex exec --experimental-json` and passes the prompt on stdin), and it fits the port's shape: a stream file, `render_line`, `is_progress`, a process exit code.
- `app-server --listen stdio://` is what the Python SDK wraps (`sdk/python/src/openai_codex/client.py`), a bidirectional JSON-RPC session that adds typed errors with `willRetry`, rate-limit updates, `thread/tokenUsage/updated` with `last` and `model_context_window`, and subagent threads.
  It needs a client that writes requests to stdin, so the Executor would no longer be "run a command and tail its stdout".
- A middle path: `exec --json` for control, plus reading `token_count` lines from the rollout file for windows, context and last-request usage.

## Mapping onto the port

| Port member or field | Codex source | Notes |
| --- | --- | --- |
| `name` | `"codex"` | |
| `needs().egress` | `chatgpt.com`, `auth.openai.com` (subscription); `api.openai.com` (API key); MCP hosts | analytics also goes to `chatgpt.com` unless disabled |
| `needs().credentials` | `auth.json` in `CODEX_HOME`, or `CODEX_API_KEY` | no env var carries a ChatGPT subscription token; `Credential.env` is Claude-shaped |
| `default_model` | always pass `-m` | Codex's own default follows the remote catalog |
| `settings_problem` | catalog slugs and `model_reasoning_effort` | no aliases like `sonnet`; the catalog is remote and per account; the valid efforts depend on the model, which the port checks as a pair |
| `reads()` | the kit root | plus a `HOME` to blank out |
| `kit_sha`, `assemble` | build `CODEX_HOME` with `config.toml`, `AGENTS.md`, `skills/`, `agents/*.toml` | Claude's plugin layout does not carry over; the kit becomes a home directory, not a plugin |
| `start` | `env -u CODEX_API_KEY -u CODEX_ACCESS_TOKEN CODEX_HOME=... HOME=... codex exec --json --dangerously-bypass-approvals-and-sandbox --strict-config -C <workspace> -m ... -c model_reasoning_effort=... --output-schema ... -o ... -` | prompt on stdin or argv |
| `windows()` | rollout `token_count.rate_limits` or `app-server` `account/rateLimits/updated` | not in `exec --json`; window names must be derived from `window_minutes`; `used_percent` is 0-100 while `WindowUsage.used` is a share |
| `is_progress` | `item.*`, `turn.*`, excluding `error` items | no heartbeat to exclude, no deltas |
| `render_line` ends stream | `turn.completed` or `turn.failed` | an interrupted turn ends with neither |
| `AttemptResult.output` | last `agent_message.text` parsed as JSON, or the `-o` file | no `structured_output` field |
| `AttemptResult.failure` | exit code + `turn.failed.error.message` text | typed class only via `app-server` |
| `AttemptResult.resets_at` | parse "Try again at ..." or rollout `rate_limits.*.resets_at` | |
| `Usage.cost_usd` | none | compute from a price table |
| `Usage.turns` | none | one user turn per `exec`; model request count not reported |
| `Usage.input_tokens` | `usage.input_tokens - usage.cached_input_tokens` | Codex's input includes cached; Claude's `input_tokens` excludes cache reads |
| `Usage.output_tokens` | `usage.output_tokens` | includes reasoning (**unverified**; Responses API counts reasoning inside output) |
| `Usage.cache_read_tokens` | `usage.cached_input_tokens` | |
| `Usage.cache_write_tokens` | `usage.cache_write_input_tokens` | defaults to 0 when absent |
| `ModelTokens` by model | one entry for `-m` | no per-model split; a "model rerouted" item means the split is wrong |
| `ModelTokens.thinking_tokens` | `usage.reasoning_output_tokens` | Claude has no such field today |
| `AttemptStats.api_requests` | none in `exec` | |
| `AttemptStats.turns` | none | |
| `AttemptStats.duration_ms` | wall clock of the process | `duration_api_ms` has no equivalent |
| `AttemptStats.subagents_started` | count `collab_tool_call` with `tool = spawn_agent` | |
| `AttemptStats.subagents_failed` | `agents_states.*.status = errored` | **unverified** that it is final in the completed item |
| `AttemptStats.subagents_by_type` | none in `exec` | `spawn_agent` items carry no role name |
| `AttemptStats.tool_calls` | count items by `command_execution`, `file_change`, `mcp_tool_call:<server>/<tool>`, `web_search` | Codex has no named built-in tools like `Read` or `Edit` |
| `AttemptStats.tool_errors` | items with `status = failed` | `command_execution` with a non-zero `exit_code` still has `status = completed` or `failed` (**unverified** which) |
| `AttemptStats.permission_denials` | `command_execution.status = declined` | none expected under `--yolo` |
| `AttemptStats.peak_context_tokens` | rollout `token_count.info.last_token_usage` | not in `exec --json` |
| `AttemptStats.context_window` | rollout `token_count.info.model_context_window` | not in `exec --json` |
| `AttemptStats.utilization_start/end` | first and last rollout `token_count.rate_limits` | not in `exec --json` |
| `agent_config` check | none | no init event; the port's init-set check is Claude-specific |

## Open questions

- Exact `token_count` line shape in the rollout file, and whether `primary`/`secondary` map to five-hour and weekly with `window_minutes` 300 and 10080 on Plus and Pro; needs a live run.
- Whether the API-key path returns any `x-codex-*` rate-limit headers.
- How the factory's Sandbox proxy can own the ChatGPT refresh token, given single-use refresh tokens; copying `auth.json` per Run is unsafe for parallel Runs.
- SIGTERM exit status, clap usage-error status, and the 401 message text.
- Which JSON Schema keywords of the factory's output schemas strict mode rejects.
- Whether `exec resume` usage totals include earlier turns.
- Whether to build the Codex adapter on `exec --json` plus the rollout file, or on `app-server` JSON-RPC; the second needs the port to accept a bidirectional session.
