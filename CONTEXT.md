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

**Target project**:
The repository the factory builds in, as opposed to the factory's own repository.

### Execution

**Pipeline**:
A declarative description of the Stages, Gates and Decisions a Ticket goes through and the order between them.
_Avoid_: Workflow, flow, chain

**Stage**:
A step of a Pipeline in which a Fresh agent with its own role, prompt and model works on the Ticket.
_Avoid_: Step, phase

**Gate**:
A deterministic pass/fail check after a Stage, parameterized by where it sits in the Pipeline.
A check made by a model is a Decision, never a Gate.
_Avoid_: Check, validator, hook, LLM judge

**Decision**:
A point in a Pipeline or in a Run where one outcome is chosen from a closed list declared in config, such as accept, rework or escalate.
_Avoid_: Judgment, choice, branch

**Decider**:
The implementation behind a Decision, such as a function, a large or small model, a Consilium or a human.
A Decider only returns the outcome and its reasons; the Orchestrator carries the outcome out.
_Avoid_: Judge, resolver

**Run**:
One pass of a Spec through the factory.

**Orchestrator**:
The deterministic control loop that drives a Run and is the only component that changes its state.
_Avoid_: Lead, coordinator, mayor, senior agent

**Cockpit**:
An interactive agent session through which the owner watches and steers a Run by way of the Orchestrator's control interface.
_Avoid_: Control room, console, dashboard

### Agents

**Fresh agent**:
An agent started with empty context that receives only pointers to the Ticket and its artifacts.
_Avoid_: Clean agent, new session

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
- Wayfinder decision tickets on this repo's own tracker are planning artifacts and are not Tickets in the factory sense.
