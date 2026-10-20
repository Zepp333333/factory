# How the engine sets up a Workspace and an agent that are visible in Orca

Issue: #5, map: #1.
Date: 2026-10-19.
Source: the Orca sources, `main` at commit [`59a9da2`](https://github.com/stablyai/orca/tree/59a9da22a0c6086a86c223534b36a6498eaf1b0a) (2026-10-19), latest release [v1.4.230](https://github.com/stablyai/orca/releases/tag/v1.4.230).
Below, `B` stands for `https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a`.
The terms Spec, Ticket, Workspace, Sandbox, Run, Orchestrator are used in the sense of `CONTEXT.md`.
The Orca words "workspace", "worktree", "Run", "Task" are written in quotes with the Orca prefix, so they are not confused with ours.

## Short answer

Orca has a public external interface for orchestration - the `orca` CLI with the `--json` flag, which talks to the running app over a local unix socket.
The Python engine should call `orca worktree create`, `orca agent launch`, `orca terminal create|read|send|wait` and `orca worktree set` through `subprocess`, rather than write worktrees and Orca configs itself.
Orca determines the "agent needs input" status itself, from the agent's managed hooks and from OSC 7501 in the PTY stream, if the agent is running in an Orca terminal.
There is no native attach to a process inside Docker, but there are two working paths: `docker exec -it` inside an Orca terminal, or a per-workspace environment recipe (Docker with `sshd`, Orca connects over SSH).

## 1. How Orca discovers worktrees

- An Orca worktree is a real `git worktree`, plus Orca metadata (terminals, tabs, UI state) ([B/skill-guides/orca-cli.md](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/skill-guides/orca-cli.md), section Worktrees).
- A worktree's address in Orca is the pair `<repoId>::<worktreePath>`; the selectors are `id:`, `path:`, `branch:`, `name:`, `active` (same place).
- Orca reads the list of worktrees from `git worktree list --porcelain` of the registered repositories ([B/src/main/git/worktree-list-reader.ts](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/src/main/git/worktree-list-reader.ts), [B/src/shared/git-worktree-porcelain-parser.ts](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/src/shared/git-worktree-porcelain-parser.ts)).
- The repository must be added to Orca (`orca repo add --path /abs/repo --json`).
- Worktrees created outside Orca (`git worktree add`) count as external and are hidden by default for repositories added after 2026-05-23; they can be shown through `orca repo set --repo id:<repoId> --external-worktree-visibility show --json` or in the UI "Non-Orca worktrees" ([B/docs/site/content/docs/model/worktrees.mdx](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/docs/site/content/docs/model/worktrees.mdx), section "Using plain git"; [B/src/shared/external-worktree-visibility.ts](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/src/shared/external-worktree-visibility.ts)).
- The settings have separate sources of external worktrees: Claude Code worktrees, GSD worktrees, other ones and user-defined absolute paths (same place).
- Orca notices a `git worktree remove` made outside it and cleans up its state on the next refresh (same place).
- The root of Orca worktrees is `~/orca/workspaces` by default ([B/src/shared/constants.ts](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/src/shared/constants.ts), `getDefaultWorkspaceDir`), nested by repository name (the `nestWorkspaces` setting).
- This matches the owner's local layout: `~/orca/workspaces/factory/shag` is a worktree of the `factory` repository (its `.git` file points to `~/projects/factory/.git/worktrees/...`).
- `orca worktree create` does not accept a path or a branch name: the path comes from `workspaceDir`, and the branch is derived from `--name` ([B/src/cli/specs/core.ts](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/src/cli/specs/core.ts), `worktree create`).
  An explicit branch name exists only in the UI (Advanced, Branch name) ([worktrees.mdx](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/docs/site/content/docs/model/worktrees.mdx), "Naming the branch").

## 2. How Orca sees agents and their sessions

- An agent session in Orca is one CLI agent in one terminal of one worktree ([B/docs/site/content/docs/model/agents-sessions.mdx](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/docs/site/content/docs/model/agents-sessions.mdx)).
- The status arrives over two channels ([B/src/main/agent-hooks/README.md](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/src/main/agent-hooks/README.md)):
  - Managed hooks: Orca installs its own hooks into the agent's config (Claude Code, Codex and others); they POST to Orca's loopback hook server and carry state, prompt, tool, answer, provider session id, model, subagents.
  - OSC 7501 (Program Status Protocol): the agent itself writes its status into the terminal stream, and Orca reads it from the PTY output and answers the probe `OSC 7501 ; ?`.
    It carries state, the blocking reason (`permission`, `question`, `auth`), one line of text and child entries.
- Hooks take precedence, and OSC 7501 fills the gaps, including when hooks are not installed or the agent was started by hand (same place).
- Claude Code supports OSC 7501 since version 2.1.295, Codex since 0.163.0 (the table in the same README).
- Managed hooks work only inside an Orca terminal: the script exits silently if there is no `ORCA_PANE_KEY` and no endpoint from `ORCA_AGENT_HOOK_ENDPOINT` ([B/src/main/agent-hooks/hook-stdin-contract.ts](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/src/main/agent-hooks/hook-stdin-contract.ts), [B/src/main/agent-hooks/hook-script-outside-orca.test.ts](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/src/main/agent-hooks/hook-script-outside-orca.test.ts)).
- Consequence: an agent the engine starts in its own PTY or `subprocess` is not visible in Orca as an agent; it must live in an Orca terminal.
- If the agent is not started through the agent combobox or the CLI but typed by hand, the indicators may not show up ([agents-sessions.mdx](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/docs/site/content/docs/model/agents-sessions.mdx), "Cards").
- By default Orca starts Claude with `--dangerously-skip-permissions` and Codex with `--dangerously-bypass-approvals-and-sandbox`; this is changed in Settings, Agents, launch arguments (same place, "Launch defaults").
- There is full-text search across sessions, `orca search`, only if a human has turned it on in Settings ([orca-cli.md](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/skill-guides/orca-cli.md), "Agent Session Search").

## 3. The interface for external orchestration

### The `orca` CLI

- The CLI ships with the app and is registered in Settings, General, Orca CLI; on Linux the command is called `orca-ide` ([B/docs/site/content/docs/cli/overview.mdx](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/docs/site/content/docs/cli/overview.mdx)).
- The documentation states directly that the CLI is meant for scripts and agents "from any shell" (same place).
- Transport: the CLI reads runtime metadata (`orca-runtime.json` in the profile) and sends JSON RPC over a unix socket or named pipe ([B/src/cli/runtime/transport.ts](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/src/cli/runtime/transport.ts)).
  This protocol is internal and versioned together with the app, so it is the CLI that should be relied on, not the socket.
- If Orca is not running, the CLI says so, and it is started with `orca open --json` ([B/skills/orca-cli/SKILL.md](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/skills/orca-cli/SKILL.md)).
- The commands the engine needs ([orca-cli.md](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/skill-guides/orca-cli.md), [B/src/cli/specs/agent-launch.ts](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/src/cli/specs/agent-launch.ts)):

```text
orca status --json
orca repo add --path /abs/target-project --json
orca worktree create --repo id:<repoId> --name <ticket-slug> --no-parent --base-branch <integration-branch> --setup skip --comment "<ticket>" --json
orca agent launch --agent claude --worktree id:<repoId>::<path> --prompt "<brief>" --model <id> --effort <level> --json
orca terminal create --worktree id:<repoId>::<path> --title "<stage>" --command "<argv>" --json
orca terminal read --terminal <handle> --cursor <cursor> --json
orca terminal send --terminal <handle> --text "<text>" --enter --wait-submit 10 --json
orca terminal wait --terminal <handle> --for tui-idle --timeout-ms 300000 --json
orca terminal wait --terminal <handle> --for exit --timeout-ms 5000 --json
orca worktree set --worktree id:<repoId>::<path> --comment "<status>" --workspace-status in-review --unread --json
orca worktree ps --json
orca worktree rm --worktree id:<repoId>::<path> --force --json
```

- `worktree create --agent` and `agent launch` return `result.launch.outcome.handle` (the agent's only handle) and an `operationId` for an idempotent retry through `--retry-launch` (same place, "Starting agents").
- Handles live within one runtime; after an Orca restart the list has to be read again and work continues with the new handle (same place).
- `terminal send` with `--enter` gives a receipt with the stages `input_accepted` and `turn_started`; `accepted: true` does not prove that the turn has started (same place, "Terminals").
- `worktree set --comment` is the status line on the card, `--workspace-status` is the column (`todo`, `in-progress`, `in-review`, `completed`), and `--unread` is the "needs human attention" dot (same place, "Worktree Comments").
  This is a ready-made channel through which the Orchestrator can show the owner the Stage and Gate of each Ticket.

### Orca orchestration

- Orca has its own coordination layer: "Run", "Task", "Dispatch", inbox, `ask`/`reply`, `worker_done`, a dependency DAG ([B/skill-guides/orchestration.md](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/skill-guides/orchestration.md)).
- It assumes that the coordinator is an agent in an Orca terminal, and that a worker gets an injected preamble and sends lifecycle messages itself through `orca orchestration ...`.
- For our engine this overlaps with its own model of Pipeline, Stage, Gate and Orchestrator; taking it as the core would mean handing Orca ownership of the lifecycle.
- The useful part is `orca orchestration send/ask`, if a channel for the agent's questions to the Orchestrator is ever needed, but that decision belongs to a separate issue.

### Config files

- `orca.yaml` at the root of the Target project sets setup scripts, `worktree.sharedDirectories` and `environmentRecipes`; `.worktreeinclude` lists gitignored files to copy into a new worktree ([worktrees.mdx](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/docs/site/content/docs/model/worktrees.mdx), [B/docs/site/content/docs/model/orca-yaml.mdx](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/docs/site/content/docs/model/orca-yaml.mdx)).
- There is no file through which an external process registers a worktree or an agent without the CLI.

### Remote runtimes

- Remote Orca Server (`orca serve`) holds worktrees, terminals and agents on a separate machine, and clients connect to it ([B/docs/site/content/docs/ways-to-run.mdx](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/docs/site/content/docs/ways-to-run.mdx)).
- SSH targets: `git worktree` and the agents run on the remote host, the UI locally (same place).

## 4. How Orca shows "agent needs input"

- The "amber question mark" glyph on the tab and in the worktree row, and the "Needs You" counter in the sidebar ([agents-sessions.mdx](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/docs/site/content/docs/model/agents-sessions.mdx), "State indicators").
- The "Needs You" column in the experimental Agent Dashboard, where the card is highlighted and can show the text of the question (same place, "Agent dashboard").
- Notifications on the transition from working to idle: system notification, sound, chip, bell, Dock badge ([B/docs/site/content/docs/notifications.mdx](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/docs/site/content/docs/notifications.mdx)).
- The source of the state is hooks and OSC 7501; `blocked` with `kind` `permission` or `question` is displayed as `waiting` ([B/src/shared/program-status/agent-status-projector.ts](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/src/shared/program-status/agent-status-projector.ts), `blockedState`).
- The engine can explicitly ask for the owner's attention through `orca worktree set --unread` and `--comment`, regardless of the agent's state.
- The guides studied have no public CLI command "give me the agent's waiting/working status"; the engine has `terminal wait --for tui-idle|exit` and reading the output.
  For orchestration workers there is `worker-list` with `projection.liveness` and `attention` ([orchestration.md](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/skill-guides/orchestration.md)).

## 5. An agent inside Docker

There is no native Docker integration (attaching to a container, `docker exec` from Orca) in the sources: `src/main` has no code that calls `docker exec`, and the documentation mentions Docker only in the context of environment recipes.

### Path A: `docker exec -it` in an Orca terminal

- The engine starts the Sandbox container itself, with a bind mount of the Workspace at the same absolute path, then runs `orca terminal create --worktree <id> --command "docker exec -it <ctr> claude ..." --json` ([orca-cli.md](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/skill-guides/orca-cli.md), "Terminals"; [B/skill-guides/orchestration/references/low-level-topology.md](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/skill-guides/orchestration/references/low-level-topology.md)).
- The terminal, the output and `terminal read/send/wait` work, because it is an ordinary Orca PTY.
- Conclusion (not checked by hand): the "Needs You" status will arrive only through OSC 7501, since `docker exec -it` passes through the TTY and escape sequences, while managed hooks inside the container cannot reach the host's loopback hook server and will not get `ORCA_PANE_KEY`.
  So there will be state and the blocking reason, but no prompt, provider session id, preview or resume ([README](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/src/main/agent-hooks/README.md), "Could an agent drop its hook for OSC 7501?").
- Requires Claude Code 2.1.295 or later inside the image.
- This has to be confirmed by a prototype before it goes into the Spec.

### Path B: per-workspace environment recipe with Docker and SSH

- The experimental "Cloud VM" feature: `environmentRecipes` in `orca.yaml` describes the scripts `create`, `suspend`, `resume`, `destroy`; Orca creates an environment for each worktree ([ways-to-run.mdx](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/docs/site/content/docs/ways-to-run.mdx), section 4; [B/skill-guides/orca-per-workspace-env.md](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/skill-guides/orca-per-workspace-env.md)).
- For local Docker there is a ready-made template: an image with `sshd` and the agent, `docker run -p 127.0.0.1::22`, `create` prints `connection.type: "ssh"`, and Orca connects to it as to an SSH target ([B/skill-guides/orca-per-workspace-env/references/docker-ssh.md](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/skill-guides/orca-per-workspace-env/references/docker-ssh.md)).
- The alternative is `orca serve` inside the container and a pairing code (Orca-server mode) ([orca-per-workspace-env.md](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/skill-guides/orca-per-workspace-env.md), section 8).
- Plus: Orca sees such an agent as a native one, with hooks, because Orca installs hooks on remote hosts ([B/src/main/agent-hooks/remote-managed-hook-installers.ts](https://github.com/stablyai/orca/blob/59a9da22a0c6086a86c223534b36a6498eaf1b0a/src/main/agent-hooks/remote-managed-hook-installers.ts)).
- Minuses: the feature is experimental; Orca, not the engine, owns the container's lifecycle; the recipe must be in `orca.yaml` on the Target project's main branch; the worktree is created inside the environment, not on the host.
- The agent authenticates in the container through a separate auth image, not through a bind mount of the host's `~/.claude` (same place, section 4 and docker-ssh.md).

## Recommendation

1. The engine's only point of integration with Orca is a thin `OrcaClient` adapter in Python on top of `subprocess` calls of `orca ... --json`, with an `orca status --json` check at the start of a Run.
   The engine does not write Orca's state itself and does not go to its socket.
2. The Workspace is created through `orca worktree create --repo ... --name <ticket> --base-branch <integration-branch> --no-parent --json`, and the worktree id is stored in the Ticket's state.
   If the engine needs control over the path and the branch name, the fallback is `git worktree add` by the engine itself plus `orca repo set --external-worktree-visibility show`.
3. Every Fresh agent runs in an Orca terminal: without a Sandbox through `orca agent launch`, with a Docker Sandbox through `orca terminal create --command "docker exec -it ..."` (path A).
4. The Stage, Gate and outcome of a Ticket are reflected on the card through `orca worktree set --comment/--workspace-status/--unread`.
5. Do not use Orca orchestration ("Run", "Task", "Dispatch") as the engine's core; it is a competing lifecycle model.
6. Keep path B (environment recipe) as an implementation option for the Sandbox, in case path A does not give the "Needs You" status in the prototype.
7. Next step: a prototype issue "Claude Code in `docker exec -it` inside an Orca terminal shows Needs You", with a check of OSC 7501 through the container.

## Open questions

- Whether the OSC 7501 probe works through `docker exec -it` in practice (not checked).
- How Orca decides that a terminal created through `terminal create --command` is an agent terminal: by the `app` id from OSC 7501 or only by a start through agent launch (the code in `agent-status-projector.ts` takes the agent type from `app`, but the display for such terminals was not checked).
- Whether the agent's executable can be replaced in Settings, Agents with a `docker exec` wrapper, so that `agent launch` itself starts the agent in the Sandbox (the sources studied show only launch arguments).
- `CONTEXT.md` so far exists only in the working copy `~/orca/workspaces/factory/shag` and is not committed.
