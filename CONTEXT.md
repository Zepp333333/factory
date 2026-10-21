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
_Avoid_: Try, execution, session

**Gate**:
A deterministic pass/fail check after a Stage, parameterized by where it sits in the Pipeline.
A check made by a model is a Decision, never a Gate.
_Avoid_: Check, validator, hook, LLM judge

**Probe**:
The command a Gate runs in the Ticket's Sandbox to produce the reports the Gate then judges.
A Gate that only reads the Workspace or another Gate's Probe has no Probe of its own.
_Avoid_: Check command, runner

**Decision**:
A point in a Pipeline or in a Run where one outcome is chosen from a closed list declared in config, such as accept, rework or escalate.
_Avoid_: Judgment, choice, branch

**Operation**:
A deterministic point in a Pipeline where the Orchestrator itself changes a Workspace or the Run's branches, such as bringing the Integration branch into a Ticket or merging a Ticket into it.
No agent works in an Operation; a Gate only checks and never changes anything.
_Avoid_: Action, step, hook

**Merge window**:
The exclusive right of one Ticket at a time to bring in and then merge into the Integration branch; it covers only Operations and Gates, and a Ticket that enters a Stage or Decision gives it up.
_Avoid_: Merge lock, merge queue

**Decider**:
The implementation behind a Decision, such as a function, a large or small model, a Consilium or a human.
A Decider only returns the outcome and its reasons; the Orchestrator carries the outcome out.
_Avoid_: Judge, resolver

**Run**:
One pass of a Spec through the factory.

**Command**:
A request to change a Run, such as answering a Decision or cancelling a Ticket, that the Orchestrator accepts or rejects; it comes from the CLI, the Run's terminal or a Cockpit, and never changes the Run directly.
_Avoid_: Message, signal, action

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
