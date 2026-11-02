# How the Attempt renderer fills an Orca agent card

Issue: #20.
Date: 2026-11-02.
Source: the Orca sources, `main` at commit [`7872cb5`](https://github.com/stablyai/orca/tree/7872cb5aa175fa873417748fdafe426aaf6f16fb) (2026-11-02).
The terms Attempt, Executor, Sandbox and Workbench are used in the sense of `CONTEXT.md`.

## Short answer

The Attempt renderer can fill the card itself.
It runs on the host inside the Orca pane, so it inherits the pane's hook variables and reaches the loopback hook server directly.
It posts Claude-shaped hook events built from the agent's stream, and Orca accepts and keeps them.
No hook runs in the Sandbox, and the Sandbox needs no egress to the host.
Orca shows the prompt, the current tool with its input, the last reply and the session id.
Orca does not show the model for the `claude` source.

## 1. Where the agent runs

- `OrcaWorkbench.open_attempt` starts only `factory.render` in the pane (`src/factory/workbench.py`).
- The Orchestrator starts the agent through `sbx exec`, outside any pane.
- So the agent process never sees `ORCA_PANE_KEY`, while the renderer does.

## 2. The pane's environment

- `orca terminal create` builds the pane env with `buildTerminalWorkspaceEnv` ([src/main/runtime/terminal/launch/terminal-create.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/main/runtime/terminal/launch/terminal-create.ts), line 98).
- It adds the hook variables when the hooks setting is on, which is the default ([src/main/agent-hooks/agent-hook-runtime-deps.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/main/agent-hooks/agent-hook-runtime-deps.ts), lines 54-55; [src/shared/agent-status-hooks-setting.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/shared/agent-status-hooks-setting.ts), line 14).
- The pane gets `ORCA_AGENT_HOOK_PORT`, `_TOKEN`, `_ENV`, `_VERSION`, `_TRANSPORT` and `_ENDPOINT` ([src/main/agent-hooks/server/server-runtime-env.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/main/agent-hooks/server/server-runtime-env.ts), lines 17-28).
- The pane also gets `ORCA_PANE_KEY`, `ORCA_TAB_ID` and `ORCA_WORKTREE_ID` ([src/main/ipc/pty/spawn-pane-env.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/main/ipc/pty/spawn-pane-env.ts), lines 56-66).
- `ORCA_AGENT_LAUNCH_TOKEN` is set only for an agent launch, and the server does not require it ([src/main/agent-launch/agent-launch-token.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/main/agent-launch/agent-launch-token.ts)).
- `ORCA_AGENT_HOOK_ENDPOINT` names a file `endpoint.env` with `KEY=VALUE` lines for the port, token, env, version and transport ([src/shared/agent-hook-listener/endpoint-publication.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/shared/agent-hook-listener/endpoint-publication.ts), lines 34-55).
- Orca mints a new token on every start of its hook server ([src/main/agent-hooks/server/server-lifecycle.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/main/agent-hooks/server/server-lifecycle.ts), line 55).
  So the renderer reads the endpoint file, not the env snapshot, and rereads it after a 403.

## 3. The transport

- A hook is an HTTP POST to `http://127.0.0.1:$ORCA_AGENT_HOOK_PORT/hook/<source>` with the header `X-Orca-Agent-Hook-Token` ([src/main/agent-hooks/hook-post-command.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/main/agent-hooks/hook-post-command.ts)).
- The form-encoded body carries `paneKey`, `tabId`, `worktreeId`, `env`, `version` and `payload`, the hook's JSON.
- The raw-JSON transport sends the payload as the body and packs the same fields into a base64 header `X-Orca-Agent-Hook-Meta`.
- The list of sources is fixed, with no generic source ([src/shared/agent-hook-relay.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/shared/agent-hook-relay.ts), lines 15-44).
  The `claude` source fits the factory's Claude Code Executor.

## 4. What the server checks

- A wrong token gives 403, and an unknown source gives 404 ([src/main/agent-hooks/server/server-lifecycle.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/main/agent-hooks/server/server-lifecycle.ts), lines 65-74 and 128-133).
- The pane key must parse, `tabId` must match the pane key's tab, and the payload must be a JSON object ([src/shared/agent-hook-listener/hook-envelope.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/shared/agent-hook-listener/hook-envelope.ts), lines 163-180).
- A wrong `env` or `version` only logs a warning (same file, line 172).
- The server checks the agent's process only when the post names one in `agentProcess` ([src/shared/agent-hook-owner-presence.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/shared/agent-hook-owner-presence.ts), lines 42-44; [src/main/agent-hooks/server/server-agent-presence.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/main/agent-hooks/server/server-agent-presence.ts), lines 129-131).
- The renderer leaves `agentProcess` empty.
  A python process would fail the check, because Orca matches its argv against known agents ([src/shared/hook-process-root-evidence.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/shared/hook-process-root-evidence.ts), lines 80-121).
- Orca retires a row when a known agent process exits, so a renderer's row lives until the pane closes ([src/renderer/src/components/terminal-pane/pty-connection/confirmed-process-exit-row-retire.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/renderer/src/components/terminal-pane/pty-connection/confirmed-process-exit-row-retire.ts), lines 18-24).

## 5. The events and fields

| Card field    | Event                             | Payload field                                  |
| ------------- | --------------------------------- | ---------------------------------------------- |
| Session id    | `SessionStart`, every event       | `session_id`                                   |
| Prompt        | `UserPromptSubmit`                | `prompt`                                       |
| Tool          | `PreToolUse`, `PostToolUse`       | `tool_name`, `tool_input`, `tool_use_id`       |
| Tool output   | `PostToolUse`                     | `tool_response`                                |
| Last reply    | `Stop`                            | `last_assistant_message`                       |
| Subagents     | `SubagentStart`, `SubagentStop`   | `agent_id`, `agent_type`                       |

- Sources: [src/shared/agent-hook-listener/prompt-fields.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/shared/agent-hook-listener/prompt-fields.ts), lines 42-55; [src/shared/agent-hook-listener/providers/claude-tool-fields.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/shared/agent-hook-listener/providers/claude-tool-fields.ts), lines 17-60.
- `SessionStart` takes `source: "startup"`.
- Without `last_assistant_message`, Orca reads the reply from `transcript_path` on disk ([src/shared/agent-hook-listener/providers/claude-tool-fields.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/shared/agent-hook-listener/providers/claude-tool-fields.ts), lines 52-60).
  So `Stop` always carries the reply, and the posts leave `transcript_path` out, because that file lives in the Sandbox.
- Orca also feeds a given `transcript_path` to its History ([src/main/agent-hooks/server/server-ingest-normalization.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/main/agent-hooks/server/server-ingest-normalization.ts), lines 150-154).
- The renderer sends no `SessionEnd`.
- The status built for `claude` has no model field ([src/shared/agent-hook-listener/providers/claude-status-build.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/shared/agent-hook-listener/providers/claude-status-build.ts), lines 38-57).
  Only the `codex` and `pi` sources read `model`.

## 6. Hooks and OSC 7501

- While a hook owns a pane's row, an OSC 7501 report on that pane adds only a facet with state, kind, progress and message ([src/main/agent-hooks/server/server-ingest-terminal.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/main/agent-hooks/server/server-ingest-terminal.ts), lines 212-252; [src/shared/program-status/facet.ts](https://github.com/stablyai/orca/blob/7872cb5aa175fa873417748fdafe426aaf6f16fb/src/shared/program-status/facet.ts), lines 12-22).
- OSC 7501 children do not survive under a hook row.
- The factory's Run tree comes from `factory run` in the owner's terminal, and the renderer emits no OSC 7501 (`src/factory/run_view.py`, `src/factory/render.py`).
  So the Attempt card and the Run tree live in different panes and do not compete.

## Recommendation

- The Executor turns its stream into neutral card facts: prompt, tool and input, tool output, reply, session id, subagents, turn end.
- The Orca adapter maps these facts to `claude` hook events and posts them from the renderer.
  The renderer does this only when `OrcaWorkbench` starts it, so the core and the plain terminal stay unaware of Orca.
- A post never blocks or fails the renderer.
  It uses short timeouts, rereads the endpoint file after a 403, and skips silently without the pane variables.
- The renderer keeps its own turn state and sends `Stop` when the turn ends, including on a crash of the agent, or the card stays on "working".
- The model stays out of the card.

## Risks

- The hook payloads are a private interface of Orca and change often.
  Pin the fields in one module of the adapter, so a change in Orca touches one place.
- The card is labelled Claude, and Orca may offer to resume a `session_id` that lives only in the Sandbox.
- A crashed renderer leaves the row until the pane closes.
