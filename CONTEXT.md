# Software Factory

An engine that takes a Spec, runs its Tickets through configurable Pipelines of fresh agents in parallel, and lands the result on an integration branch, driven by an Orchestrator.

## Language

### Work

**Spec**:
The large shared specification of a feature or product, published to the issue tracker.
_Avoid_: PRD, plan, specification document

**Ticket**:
A tracer-bullet vertical slice of a Spec that travels through one Pipeline inside one Workspace.
_Avoid_: Task, issue, story, job

**Frontier**:
The Tickets of a Spec that are open and no longer blocked by other Tickets.
A Run works on at most `max_parallel` Tickets of its Frontier at once, as its project file sets.

**Integration branch**:
The single branch where the finished Tickets of one Spec are merged.

**Tracker**:
The place a Spec and its Tickets are read from and where a Run reports its progress, such as GitHub Issues, GitLab Issues or a local folder.
A Run reads the Spec once at its start and does not see later edits.
_Avoid_: Backlog, board

**Target project**:
The repository the factory builds in, as opposed to the factory's own repository.

**Release channel**:
The way a Target project's changes reach its users, such as a redeployed service or a shipped app binary; it decides whether a change can be rolled back after release.
_Avoid_: Deploy target, platform

### Execution

**Pipeline**:
A declarative description of the Stages, Gates and Decisions a Ticket goes through and the order between them.
_Avoid_: Workflow, flow, chain

**Stage**:
A step of a Pipeline in which a Fresh agent with its own role, prompt and model works on the Ticket.
_Avoid_: Step, phase

**Role**:
The reusable job of a Fresh agent, with its prompt, the schema of its output and its skills; several Stages and Deciders may share one Role.
_Avoid_: Persona, agent type

**Round**:
One entry of a Ticket into a Stage; a Round holds one or more Attempts, and a Stage's round limit counts Rounds.
_Avoid_: Iteration, pass, cycle

**Attempt**:
One run of a Fresh agent on one Stage of a Ticket.
A lost or failed Attempt is never resumed; the Stage starts over with a new Attempt.
An Attempt that the world loses is `AttemptLost`: its Sandbox, Workspace or branch is gone, or the Run's process died under it. It spends no `attempts` and no Round, and the next Attempt replaces it.
An Attempt that fails because its Sandbox did (`sandbox_start`, `sandbox_lost`) spends no `attempts` of the Stage and no Round.
The Ticket's count of Sandbox losses, shared with its lost Probes, is capped by `sandbox_retries` in `defaults`, and a Stage past the cap ends `failed`.
An Attempt whose agent reports no init event, or one that differs from the Agent kit, fails with `agent_config`, a failure class like `auth`, `sandbox_start`, `executor_crash`, `timeout` and `stalled`; `stalled` means the agent made no new message or tool result for longer than the Stage's `stall`, and it is retried like `timeout`.
_Avoid_: Try, execution, session

**Gate**:
A deterministic pass/fail check after a Stage, parameterized by where it sits in the Pipeline.
A check made by a model is a Decision, never a Gate.
_Avoid_: Check, validator, hook, LLM judge

**Probe**:
The command a Gate runs in the Ticket's Sandbox to produce the reports the Gate then judges.
A Gate that only reads the Workspace or another Gate's Probe has no Probe of its own.
A Probe the Sandbox lost gives no verdict; it counts as a Sandbox loss of the Ticket, and a Gate past `sandbox_retries` escalates.
A Gate has no `attempts` of its own: the `attempt` in its artifact key counts the Probes of the Gate since the Ticket entered it, one per started or borrowed Probe, a lost one included, not Attempts of a Fresh agent, because a Gate runs no agent.
_Avoid_: Check command, runner

**Decision**:
A point in a Pipeline or in a Run where one outcome is chosen from a closed list declared in config, such as accept, rework or escalate.
A Run has one Decision of its own, `infra_failure` with the outcomes `retry` and `abort`, which opens when Attempts of two different Tickets fail in a row with the same cause class of `auth`, `sandbox_start`, `executor_crash`, `timeout` or `stalled`; `factory decide <run> @run <outcome>` answers it, `retry` spends no Round or attempt, and `abort` ends the Run as `aborted`.
_Avoid_: Judgment, choice, branch

**Operation**:
A deterministic point in a Pipeline where the Orchestrator itself changes a Workspace or the Run's branches, such as bringing the Integration branch into a Ticket or merging a Ticket into it.
No agent works in an Operation; a Gate only checks and never changes anything.
_Avoid_: Action, step, hook

**Merge window**:
The exclusive right of one Ticket at a time to bring in and then merge into the Integration branch; it covers only Operations and Gates, and a Ticket that enters a Stage or Decision gives it up.
Tickets get the window in the order they ask for it.
_Avoid_: Merge lock, merge queue

**Decider**:
The implementation behind a Decision, such as a function, a large or small model, a Consilium or a human.
A Decider only returns the outcome and its reasons; the Orchestrator carries the outcome out.
_Avoid_: Judge, resolver

**Run**:
One pass of a Spec through the factory.
Its Orchestrator beats a heartbeat into the Journal, and a Run that has not beaten for a minute is `orphaned` by any other Orchestrator, which frees its Slots.
Ctrl-C and `kill` leave the Run orphaned at once.
Only `factory resume <run>` takes an orphaned Run up again: it rebuilds the Run from the Journal, stops any agent that outlived the dead Orchestrator instead of adopting it, and loses that Attempt.
`factory finish <run>` ends an orphaned Run instead: it abandons every Ticket that did not merge, keeping its unsaved work under its abandoned ref, ends the Run `partial` and lands the merged Tickets like any `partial` Run.

**Command**:
A request to change a Run, such as answering a Decision or cancelling a Ticket, that the Orchestrator accepts or rejects; it comes from the CLI, the Run's terminal or a Cockpit, and never changes the Run directly.
The CLI sends `decide`, `cancel` (abandons a Ticket, so Tickets that depend on it never start), `override` (replaces the outcome of the Ticket's current or last Decision and stops the work after it) and `limits` (changes `max_parallel` and the round limits of Stages).
A Command sent to a Run whose process died waits in the inbox, and `factory resume` applies it on its first tick; the CLI orphans a Run with a stale heartbeat, says that the Command waits and exits, and that holds for `decide` as for the others.
_Avoid_: Message, signal, action

**Slot**:
The right of one Attempt to use the subscription; an Attempt of a Stage or of the Describer starts only once it holds a slot, and gives it back when it ends.
The factory config caps the slots that all Runs on a machine hold at once.
_Avoid_: Lease, seat, token

**Factory config**:
The file `config.yaml` in the factory's home, which sets what all Runs on a machine share, such as the slot cap.
_Avoid_: Global config, machine settings

**Orchestrator**:
The deterministic control loop that drives a Run and is the only component that changes its state.
_Avoid_: Lead, coordinator, mayor, senior agent

**Cockpit**:
An interactive agent session through which the owner watches and steers a Run by way of the Orchestrator's control interface.
_Avoid_: Control room, console, dashboard

**Workbench**:
The environment where the owner sees a Run's Workspaces and Attempts, such as Orca or a plain terminal.
It creates Workspaces and the terminals Attempts run in; where an agent actually runs is its Sandbox.
_Avoid_: Driver, UI, IDE, frontend

### Agents

**Fresh agent**:
An agent started with empty context that receives only pointers to the Ticket and its artifacts.
_Avoid_: Clean agent, new session

**Brief**:
The fixed list of pointers the Orchestrator hands a Fresh agent at the start of an Attempt, holding only the inputs its Stage declares.
_Avoid_: Context, handoff, prompt

**Artifact**:
A file a Run keeps for its Tickets, such as the output of an Attempt, a Gate verdict, a Decision outcome or the rendered Ticket itself; it never changes once written.
_Avoid_: Output, deliverable, result

**Agent kit**:
The plugin, settings, MCP servers and instructions the factory gives every Fresh agent in place of the owner's own agent config.
_Avoid_: Toolkit, bundle

**Describer**:
The Fresh agent that runs once after the last Ticket merged and writes the title and body of the Run's final PR from the skill `pr`: `Summary`, `Evidence` and `Merge Danger` with its `Door` and `Blast Radius`.
It reads the Integration branch against the base ref in a Workspace of its own and has no access to the Tracker.
The engine appends the closing of the merged Tickets and the list of the rest to its body, and a Describer that fails leaves the PR with the engine's own text.
_Avoid_: Summarizer, PR bot

**Consilium**:
A Decider in which several agents vote on one Decision, such as which code review findings are real.
_Avoid_: Panel, committee, jury

### Isolation

**Workspace**:
The git worktree that belongs to one Ticket.
_Avoid_: Checkout, sandbox

**Sandbox**:
The environment in which an agent works on a Workspace, such as a Docker container or a restricted macOS user.
_Avoid_: Container, VM, workspace

## Ambiguities

- In prior art (`implement-spec`, Superpowers, Gas Town) the orchestrator is an LLM session. Here the Orchestrator is code, and model judgment enters only through Deciders.
- In a self run the factory's own repository is the Target project, and the `factory` command comes from a stable install of `main`.
- Wayfinder decision tickets on this repo's own tracker are planning artifacts and are not Tickets in the factory sense.
- `handoff` is a field of Role `implementer`'s output, where an Attempt tells later Rounds what it did (ADR 0003).
  It reaches the next Fresh agent as an Artifact the Brief points to, and the Brief itself is never called a handoff.
