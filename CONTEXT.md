# Software Factory

An engine that takes a Spec, runs its Tickets through configurable Pipelines of fresh agents in parallel, and lands the result on an integration branch under the supervision of an Orchestrator.

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
A declarative description of the Stages a Ticket goes through and the order between them.
_Avoid_: Workflow, flow, chain

**Stage**:
One step of a Pipeline, performed either by a fresh agent with its own role, prompt and model, or by a Gate.
_Avoid_: Step, phase

**Gate**:
A deterministic check after a Stage, parameterized by where it sits in the Pipeline.
_Avoid_: Check, validator, hook

**Run**:
One pass of a Spec through the factory.

### Agents

**Fresh agent**:
An agent started with empty context that receives only pointers to the Ticket and its artifacts.
_Avoid_: Clean agent, new session

**Orchestrator**:
The senior agent over all Tickets of one Run that accepts work, convenes a Consilium and closes Workspaces.
_Avoid_: Lead, coordinator, mayor

**Consilium**:
A group of agents that vote on one question, such as which code review findings are real.
_Avoid_: Panel, committee, jury

### Isolation

**Workspace**:
The git worktree that belongs to one Ticket.
_Avoid_: Checkout, sandbox

**Sandbox**:
The environment in which an agent works on a Workspace, such as a Docker container or a restricted macOS user.
_Avoid_: Container, VM, workspace

## Ambiguities

- Wayfinder decision tickets on this repo's own tracker are planning artifacts and are not Tickets in the factory sense.
