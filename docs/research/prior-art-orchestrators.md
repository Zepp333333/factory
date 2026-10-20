# What to build into the engine from existing orchestrators

Date: 2026-10-19.
Question: ticket #3 (map #1).
Vocabulary: the terms Spec, Ticket, Frontier, Integration branch, Pipeline, Stage, Gate, Run, Orchestrator, Consilium, Workspace, Sandbox come from `CONTEXT.md`.

## Short answer

None of the candidates fits as the whole base of a Python engine.
We write the core (the Ticket graph, claim, Pipeline, Gate, round limit, escalation, Consilium) ourselves in Python.
The Stage executor is the unmodified `claude` binary under the owner's login, called as a subprocess (`claude -p --output-format stream-json`) or through `claude-agent-sdk` (Python, MIT), which starts the same binary.
We borrow design, not code:

- Symphony SPEC.md: the orchestrator model (claim states, reconciliation, retry with backoff, stall timeout, Workspace hooks, policy in the repo in `WORKFLOW.md`).
- Superpowers subagent-driven-development: a review loop with a limit of 5 rounds, a change of implementer and model in rounds 4-5, a ledger and "rulings" when the breaker trips.
- `implement-spec`: a Frontier over the blocking graph, an Integration branch, an implementer in its own worktree, a separate merger, a final code review.
- Gas Town: Refinery as a Bors-style merge queue with a Gate on the merged stack and bisection, escalation by severity, integration branches for an epic.
- Claude Code dynamic workflows: usable inside one Stage for fan-out (for example, a Consilium of N reviewers with voting), but not as the engine.

Beads could be considered as a ready-made task graph store with an atomic claim, but because of Dolt and the Go binary it is an external dependency, and the part we need (the ready query and the atomic claim) is a few dozen lines on SQLite.
Agent teams do not fit: they are experimental and do not work in non-interactive mode.

## Summary table

| Candidate | Primitives | Subscription | Control from Python | License | Main blocker as a base |
| - | - | - | - | - | - |
| Claude Code dynamic workflows | `agent()`, `pipeline()`, `parallel()`, `phase()`, schema output, resume within a session, 16-256 parallel agents | Yes, on all paid plans | Yes, through `claude -p` and the Agent SDK (tool `Workflow` must be allowed) | Proprietary Claude Code | No mid-run input, no shell from the script, JS without modules, resume tied to the session, pause on usage limit only in interactive mode |
| Claude Code agent teams | lead, teammates, a shared task list with dependencies and file-lock claim, mailbox, hooks `TaskCreated`/`TaskCompleted`/`TeammateIdle` | Yes | No: in `-p` and the Agent SDK teammates are not created | Proprietary | Experimental, no resume of teammates, one team per session, no nesting |
| OpenAI Symphony | Daemon: tracker polling, claim states, a Workspace per issue, hooks, `max_turns`, retry with backoff, stall/turn timeouts, reconciliation, `WORKFLOW.md` | Not applicable to Claude: Codex app-server protocol | Only as a specification; the reference implementation is in Elixir | Apache-2.0 | The agent is hardwired to Codex app-server; no review loop, Gate, Consilium; the reference implementation is "prototype... evaluation only" |
| Gas Town | Mayor, Polecats, Witness, Deacon, Refinery (merge queue), convoys, formulas/molecules, `gt escalate`, scheduler `max_polecats`, integration branches | Yes, it drives interactive `claude` in tmux | Only through the `gt` CLI; it is a full orchestrator in its own right | MIT | A competing engine on Go + tmux + Dolt; integration with the agent through `send-keys` and screen parsing, which the authors themselves call a "hack" |
| Beads | An issue graph with dependencies, `bd ready`, atomic `bd update --claim`, epic hierarchy, JSON output | Model-independent | Through the `bd --json` CLI; there is `beads-mcp` on PyPI | MIT | Dolt and a Go binary, schema migrations; the HTTP backend is experimental |
| Superpowers subagent-driven-development | A fresh implementer per task, task reviewer (spec + quality), scoped re-review, 5 rounds, model escalation, ledger, final whole-branch review | Yes, it is a skill in a Claude Code session | Only as a prompt: `claude -p "/subagent-driven-development ..."` | MIT | Implementers strictly sequential, the orchestrator is an LLM in one session, state in a markdown ledger |
| `implement-spec` (Matt Pocock) | Ticket graph, Frontier, Integration branch, implementer per worktree, merger subagent, final `code-review` | Yes, a skill | Only as a prompt | MIT | No round limit, no Gate, no escalation; the orchestrator is an LLM, state in its context |

## 1. Claude Code dynamic workflows

Source: https://code.claude.com/docs/en/workflows

### Primitives

A workflow is a JavaScript script that the runtime executes in the background.
`agent()` starts one subagent, `pipeline()` one per list item, `parallel()` a set of agents at once, `phase()` groups agents in the progress view, `log()` writes a message.
`agent()` accepts a `schema` and then returns JSON matching the schema; after 5 failed validations the call fails.
The model is set per agent, with the same selection order as for subagents.
By default up to 16 parallel agents, up to 256 through `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS`, and no more than 1000 agents per run.
A stalled agent is restarted up to 5 times with the same prompt, and the window is set by `stallMs`.
A run is resumable: finished agents return their saved result, and the first one that changed and all after it start again.

### Subscription

"Dynamic workflows are available on all paid plans, with Anthropic API access, and on Amazon Bedrock..."
On Pro they must be enabled in `/config`.
When the usage limit is reached, the run pauses and continues after the reset, but only in an interactive session with a subscription: "A run doesn't pause in non-interactive mode with `claude -p` or the Agent SDK".
So in our case an agent at the limit will fail, and retrying is left to the engine.

### Control from Python

Workflows are available in `claude -p` and in the Agent SDK.
The prompt approval is not shown there, and the call of tool `Workflow` goes through the usual permission check, so a `Workflow` or `Workflow(<name>)` rule is needed in allow.
A script can be saved in `.claude/workflows/` and given input through the global `args`.
The keyword `ultracode` from `-p` does not start a workflow; an explicit call of a saved workflow is needed.

### Blockers as a base

- "No mid-run user input": for approval between stages the docs recommend separate workflows.
- "No direct filesystem or shell access from the workflow itself": a deterministic Gate (tests, linter) cannot run from the script, only through an agent, which makes it nondeterministic.
- "No module loading": the script cannot work with git, the tracker or its own DB.
- `Date.now()` and `Math.random()` throw an exception so that resume is deterministic.
- Resume lives in the Claude Code session, not in our store.
- **[inference]** A workflow fits as the implementation of one Stage with fan-out inside (for example, a Consilium of N reviewers with voting, or a parallel audit), while the orchestration of Tickets, Gates and Workspaces must be outside.

## 2. Claude Code agent teams

Source: https://code.claude.com/docs/en/agent-teams

### Primitives

Lead, teammates as separate Claude Code sessions, a shared task list, a mailbox on JSON files in `~/.claude/teams/{team-name}/inboxes/`.
Tasks have the states pending, in progress, completed and dependencies: "a pending task with unresolved dependencies cannot be claimed".
"Task claiming uses file locking to prevent race conditions".
Hooks `TeammateIdle`, `TaskCreated`, `TaskCompleted` with exit code 2 work as a quality gate.

### Subscription and Python

The subscription works, these are ordinary Claude Code sessions.
But "In non-interactive mode with the `-p` flag, including Agent SDK sessions, Claude doesn't spawn teammates".
They cannot be controlled from Python.

### Blockers

Experimental, enabled by `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`.
Limitations from the docs: no resume for in-process teammates, "Task status can lag", one team per session, no nested teams, the lead is fixed.
The docs explicitly recommend starting with research and review, not with parallel implementation.
**[inference]** We do not take it; the idea of `TaskCompleted` hooks as a Gate is already covered by our deterministic Gates.

## 3. OpenAI Symphony (SPEC.md, WORKFLOW.md)

Sources: https://github.com/openai/symphony/blob/main/SPEC.md, https://github.com/openai/symphony/blob/main/elixir/WORKFLOW.md, https://github.com/openai/symphony/blob/main/elixir/README.md

### Primitives

SPEC.md (Draft v1, language-agnostic) describes "a long-running automation service that continuously reads work from a configured issue tracker, creates an isolated workspace for each issue, and runs a coding agent session for that issue inside the workspace".
Components: Workflow Loader, Config Layer, Issue Tracker Adapter, Orchestrator, Workspace Manager, Agent Runner, Status Surface, Logging.
Internal claim states: `Unclaimed`, `Claimed`, `Running`, `RetryQueued`, `Released`.
Attempt phases: from `PreparingWorkspace` to `Succeeded`, `Failed`, `TimedOut`, `Stalled`, `CanceledByReconciliation`.
"The orchestrator is the only component that mutates scheduling state."
Each tick: reconciliation, preflight validation of the config, choice of candidates, sorting by priority, created_at, identifier, dispatch until the slots run out.
Retry: after a clean exit, a repeat after 1 s; after a failure, `min(10000 * 2^(attempt - 1), max_retry_backoff_ms)`, with a default cap of 5 minutes.
Defaults: `max_concurrent_agents` 10, `max_turns` 20, `turn_timeout_ms` 1 hour, `stall_timeout_ms` 5 minutes, `polling.interval_ms` 30 s.
Workspace hooks: `after_create`, `before_run`, `after_run`, `before_remove`.
Tracker credentials stay on the host, and the agent gets provider-native tools that the host executes: "MUST NOT require the coding-agent child process to read raw tracker tokens".
Restart recovery without a DB: "tracker-driven and filesystem-driven".

WORKFLOW.md: YAML front matter (tracker, polling, workspace, hooks, agent, codex) plus a Liquid prompt template.
Statuses Todo, In Progress, Human Review, Merging, Rework, Done.
Every review comment blocks until a fix or "explicit, justified pushback".
Rework is "a full approach reset": close the PR, a new branch from `origin/main`.

### Subscription and Python

The Agent Runner is hardwired to Codex app-server: "Coding-agent executable that supports the targeted Codex app-server mode".
The reference implementation is in Elixir/OTP, with adapters for Linear, GitHub Issues, Jira Cloud, Asana, GitLab.
The README itself warns: "Symphony Elixir is prototype software intended for evaluation only... We recommend implementing your own hardened version based on SPEC.md".
**[inference]** The implementation is unusable for Claude on a subscription, but the SPEC is written exactly so that it gets rewritten in your own language, and the Agent Runner layer is replaced by `claude -p --output-format stream-json`.

### Blockers

Non-Goals: "General-purpose workflow engine", "Mandating strong sandbox controls".
There are no notions of a Pipeline of several Stages, Gate, Consilium, or a review round limit; all review logic lives in the `WORKFLOW.md` prompt.
There is no dependency graph between issues, only sorting by priority.
The Apache-2.0 license allows copying the text of the specification with attribution.

## 4. Gas Town and Beads

Sources: https://github.com/gastownhall/gastown (formerly steveyegge/gastown), https://github.com/gastownhall/gastown/blob/main/docs/agent-provider-integration.md, https://github.com/gastownhall/gastown/blob/main/docs/design/factory-worker-api.md, https://github.com/gastownhall/gastown/blob/main/docs/concepts/integration-branches.md, https://github.com/gastownhall/beads (formerly steveyegge/beads)

### Gas Town: primitives

Roles: Mayor (coordinator, "a Claude Code instance"), Polecats (executors), Witness (detects stuck agents in a rig), Deacon (patrol between rigs), Dogs, Refinery (merge queue).
Convoys group beads, formulas in TOML describe steps with `needs`, and molecules are their instances.
Refinery: "batches merge requests, runs verification gates, and merges to main using a Bors-style bisecting queue"; "polecats never push directly to main".
Integration branches: polecats start from the epic's integration branch, Refinery merges into it, and once all children are closed the branch lands with a single merge commit.
Escalation `gt escalate` with severity CRITICAL (P0), HIGH (P1), MEDIUM (P2), routed Deacon, Mayor, Overseer.
Scheduler `max_polecats` is only a parallelism limit; there is no retry limit.

### Gas Town: subscription and Python

The default runtime is the Claude Code CLI started in tmux, that is, the ordinary interactive `claude` under the user's login.
Control is only through the Go CLI `gt` and tmux: "Gas Town orchestrates agents through tmux and environment variables".
The authors themselves write in `factory-worker-api.md`: "Gas Town has no stable interface with AI agents. Every integration is a hack", listing tmux `send-keys` in 512-byte chunks, a regex on the `❯` prompt, scraping `~/.claude/projects/*.jsonl`, "Account rotation: macOS-only keychain token swapping".

### Gas Town: blockers

It is a complete alternative engine with its own roles, not a library.
Dependencies: Go, Dolt, `bd`, tmux, sqlite3.
Integration through screen scraping is fragile; we have structured `stream-json`.
**[inference]** Rotating OAuth tokens through the keychain contradicts the Anthropic rule "developers may not collect, store, or intermediate Claude.ai credentials or session tokens" (https://code.claude.com/docs/en/legal-and-compliance); this must not be repeated.
There is no separate code review stage.

### Beads: primitives

"Distributed graph issue tracker for AI agents, powered by Dolt."
`bd ready` gives tasks with no open blockers, `bd update <id> --claim` is "Atomically claim a task (sets assignee + in_progress)", plus `bd dep add`, hierarchical epic IDs, JSON output, collision-free hash IDs.
Storage modes: embedded Dolt (one writer) or `dolt sql-server` for several writers.

### Beads: Python and blockers

Control through `bd --json` from a subprocess; PyPI has only `beads-mcp`.
The HTTP backend `bd serve` is marked "EXPERIMENTAL: pin an exact beads version".
There is a schema version guard and migrations that must run in one "designated clone".
**[inference]** For one host and one Orchestrator a distributed Dolt is overkill; the ready query and the atomic claim are written on SQLite in a single transaction.
If a tracker visible to agents is needed later, Beads can be plugged in as an adapter, but our canonical Spec tracker is GitHub Issues.

## 5. Superpowers subagent-driven-development

Source: https://github.com/obra/superpowers/blob/main/skills/subagent-driven-development/SKILL.md (MIT)

### Primitives

A fresh implementer subagent per task; subagents "should never inherit your session's context or history".
One task reviewer with two verdicts: spec compliance and task quality.
Fix loop: "Five rounds maximum per task"; rounds 1-3 resume the original implementer, rounds 4-5 use a "fresh implementer on a more capable model".
Re-review is scoped: it checks only whether the findings were addressed and whether the fix diff broke something new.
Breaker after round 5: the controller itself adjudicates every open finding (park it with a ruling, or a minimal solution for a load-bearing one).
A ledger in a git-ignored directory survives compaction; every decision is written as `Ruling: <what> - <why> - <cost of being wrong>`.
It stops for a human only in four cases: an irreversible operation, security, a side effect outside the worktree (merge, push, publish), a plan broken so badly that any path is a guess.
A final whole-branch review on the strongest model, one wave of fixes.
Model selection: the cheapest model that can cope, but mid-tier as the floor for reviewers, because "Turn count beats token price".
Artifacts are passed as files (brief, report, review package), not pasted into the prompt.

### Subscription and Python

This is a skill run in a Claude Code session, so the subscription works.
From Python only by starting a session with the skill, with no programmatic access to its state.

### Blockers

"Never dispatch multiple implementation subagents in parallel (conflicts)": there is no parallelism.
The orchestrator is an LLM, and the round limit and model choice rest on prompt discipline, not on code.
**[inference]** We take the mechanics as the spec of the default Pipeline: an implement, review, fix loop with a limit, model escalation, scoped re-review, ledger rulings; but we move the round counter and model choice into the engine's deterministic code.
The prompts `implementer-prompt.md`, `task-reviewer-prompt.md`, `re-review-prompt.md` can be adapted under MIT with attribution.

## 6. `implement-spec` (Matt Pocock)

Source: the local plugin `mattpocock-skills` 1.3.1, `skills/engineering/implement-spec/SKILL.md` (MIT, Copyright 2026 Matt Pocock).

### Primitives

Tickets are "a task graph with blocking relationships", and there is always a "frontier of tickets which are ready to be grabbed".
An Integration branch, an implementer subagent per Ticket "in its own worktree on its own branch", which syncs with the integration branch, builds through `tdd` and merges the integration branch tip into its own before handing in.
A separate merger subagent moves the work into the integration branch, after which implementers start on the new Frontier.
At the end, a `code-review` over the integration branch, and one implementer fixes all findings.
Communication through "context pointers", not copying.
`disable-model-invocation: true`: only the user starts it.

### Subscription and Python

A skill in Claude Code, the subscription works; from Python only as a prompt.

### Blockers

No round limit, no Gate, no escalation to a human, no Consilium.
The graph state lives in the context of the LLM orchestrator.
**[inference]** It is the closest to our model in vocabulary (Spec, Ticket, Frontier, Integration branch already match `CONTEXT.md`), and its steps become the skeleton of a Run that the engine executes deterministically.

## What to write ourselves

- The Run state store: Tickets, dependencies, claim, attempts, rounds, rulings (SQLite).
- A Frontier scheduler with parallelism limits, retry with backoff, stall and turn timeouts, reconciliation modeled on the Symphony SPEC.
- Workspace Manager: a git worktree per Ticket from the Integration branch, hooks as in Symphony.
- A Sandbox wrapper around starting `claude` (Docker or a separate macOS user); none of the candidates provides it, and Symphony puts it in Non-Goals.
- Agent Runner: `claude -p --output-format stream-json` or `claude-agent-sdk`, event parsing, `--json-schema` for verdicts, `--permission-prompts none` for unattended runs.
- Pipeline as declarative YAML with Stages and Gates; Gates are deterministic commands (tests, linter, typecheck) that the engine runs, not the agent.
- Round limit and model escalation modeled on Superpowers, escalation to a human by severity modeled on Gas Town.
- Consilium: a fan-out of N reviewers with structured output and voting; it can be implemented in the engine or as a saved workflow inside one Stage.
- Merge into the Integration branch: sequential, with a Gate on the merged result; batching and bisection as in Refinery come later, if needed.

## Subscription risks

Anthropic: "Advertised usage limits for Pro and Max plans assume ordinary, individual usage of Claude Code and the Agent SDK" (https://code.claude.com/docs/en/legal-and-compliance).
Allowed: the end user logs in with their own subscription in "the unmodified Claude Code binary".
Forbidden: "developers may not collect, store, or intermediate Claude.ai credentials or session tokens"; the Agent SDK for other people's products only with an API key (https://code.claude.com/docs/en/agent-sdk/overview).
`--bare` does not use the subscription: "bare mode doesn't use your subscription login" (https://code.claude.com/docs/en/headless).
**[inference]** The engine must start the unmodified `claude` without `--bare`, without touching tokens, and respect the limits itself: pause the Run on `rate_limit` in `system/api_retry`, because the automatic pause on usage limit does not work in `-p`.

## Open questions

- The Python SDK `claude-agent-sdk` or a bare `claude -p` subprocess: both start the same binary; the choice is a separate decision (ADR).
- Whether SQLite is enough for the store or a tracker visible to agents is needed (Beads, GitHub Issues).
- Not tested in practice: how workflows behave in `-p` on a subscription and the format of rate limit errors on the Max plan.
