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
The id of a Ticket or an issue is opaque text that only the Tracker reads, which shows it in a text as its reference, such as `#12` on GitHub and the bare id in a folder.
A Tracker serves its consumers through roles: every Tracker is a SpecSource and a Closing, and one that can is a TicketBoard, an IssueDesk and Discussions.
_Avoid_: Backlog, board

**SpecSource**:
The role of a Tracker that reads the Spec and its Tickets and tells where the Spec lives, for `factory run`.

**Closing**:
The role of a Tracker that writes the PR text in its words: the fallback text, the footer that closes the merged Tickets, the Describer's text defused and the reference of an id.

**TicketBoard**:
The role of a Tracker that comments on, labels and assigns a Ticket or its Spec, for the projection of a Run and the Tracker Operations.
A Run on a Tracker without one projects nothing, and a Pipeline or Run graph with a Tracker Operation fails `factory check` on it.

**IssueDesk**:
The role of a Tracker that finds and files issues, for the Operation `file_issues`.
A Pipeline with `file_issues` fails `factory check` on a Tracker without one.

**Discussions**:
The role of a Tracker that reads what people wrote: the comments of a Ticket, and the conversation and review threads of a PR with their path, line and thread where they have them.
It tells the owner, the account whose access the Tracker works with, and the factory's own comments, which the factory signs because it posts them with that same access.
A Pipeline or Run graph with a Tracker input fails `factory check` on a Tracker without Discussions, as on a Spec folder.

**Tracker input**:
An input of a Stage that the Tracker holds, `{tracker: ticket_comments}` or `{tracker: pr_comments}`, whose `authors` default to the owner and which holds none of the factory's own comments but those that the Operations of its `posted_by` posted in this Run.
The Brief holds the comments the Stage has not handled yet, and the `AttemptFinished` of a done Attempt journals as `handled` the ones its Brief held, so a failed Attempt leaves them new and the next entry into the Stage gets only later ones.
A Tracker that cannot be read holds the Stage and spends no Attempt and no Round, and the Run tries again.
`pr_comments` needs a node some path reaches after `open_pr`, and a Run Stage has no Ticket to read `ticket_comments` of.
No node can run `pr_comments` yet, because the only Run Stage is the Describer, which comes before `open_pr`.

**Review loop through the Tracker**:
The Library's Pipeline `review-via-tracker`, in which a `comment` Operation posts the reviewer's summary and findings on the Ticket, the fixer reads them as a Tracker input with `posted_by` and answers each one, a `comment` Operation posts the answers as replies, and the reviewer looks again, up to the `rounds` of its Stage.
A reply is a comment that names the `id` it answers, because the port `TicketBoard` posts a comment on the Ticket and not in a thread.
The function Decider `findings` ends the loop at a review without findings.

**PullRequests**:
The code host where a Run lands its Integration branch as a PR, picked from the git remote of the Target project and apart from the Tracker.
A project with no remote to land through keeps the PR as a file, through the kind `folder`, whose CI is absent.

**Target project**:
The repository the factory builds in, as opposed to the factory's own repository.

**Release channel**:
The way a Target project's changes reach its users, such as a redeployed service or a shipped app binary; it decides whether a change can be rolled back after release.
_Avoid_: Deploy target, platform

### Execution

**Pipeline**:
A declarative description of the Stages, Gates and Decisions a Ticket goes through and the order between them.
_Avoid_: Workflow, flow, chain

**Run graph**:
The declarative description of the flow of a Run, an item of the Library in the format of a Pipeline, whose nodes are Run nodes (ADR 0014).
The project file names it with `run:`, and `default` is the Run the factory ships: every Ticket read, the work on them, the PR, the CI of the PR, and `planned` is the same with a Planner that selects the Tickets.
A Run freezes its Run graph with the rest of its Library, and a Run from before Run graphs walks the default one.
_Avoid_: Run pipeline, Run workflow, run flow

**Run node**:
A node of a Run graph, of a kind from the closed set of its level, with outcomes that `next` routes on to another Run node or to `ended`.
It journals `RunNodeEntered` and `RunNodeEnded` with its outcome, and `factory resume` continues from the one the Journal ends in.
The brake, `budget_spent`, `infra_failure`, `cancel` and `abort` stay outside the Run nodes, as interrupts of the whole Run.
_Avoid_: Run step, phase

**Run Stage**:
A node of a Run graph that names a Role and no `kind`, and runs a Fresh agent with the settings of a Stage, whose changes nobody commits.
What it leaves out of those settings comes from the `defaults` of the Ticket Pipeline.
One is the Describer's `describe` of the default Run graph: it ends `done` or `failed`, comes after the `tickets` node and before `open_pr`, and `overrides.nodes.describe` of the project file sets its Role and settings.
The other is the Planner's `select`, whose Role says `selects: true`.
A Pipeline file has no `describer:` any more, and `factory check` names this node for one that does.

**`open_pr` node**:
The Run node that pushes the Integration branch and opens the Run's PR, which ends `opened`, or `failed` when no Ticket merged or a step failed.
It takes the Describer's text when `describe` ended `done`, and the Tracker's own text otherwise, also in a Run graph without `describe`.
It replaces the Run node kind `land`, which `factory check` refuses and names `describe` and `open_pr` for.

**`select` node**:
The Run node that produces the Tickets the Run takes, their order and their dependencies, as an Artifact whose content its event `TicketsSelected` carries, and which the `tickets` node works from.
Kind `all` takes every Ticket the Tracker read, every open one of the Spec or those of `--tickets`, and kind `filter` takes those that carry every label of `with.label` and none of `with.without`.
A Run Stage of the Role `planner` lets the Planner take them.
Its outcomes are `selected` and `empty`, and the Planner's also `failed`; a Run graph selects at most once, before `tickets`, and one without such a node takes every Ticket read.
A Ticket blocked by one the Run did not take stays queued and never starts, where `all` refuses such a Run before it starts.
The `tickets` node starts the Tickets in the order `select` gave, within the Frontier.
_Avoid_: pick, triage

**`tickets` node**:
The Run node that runs the Ticket Pipeline it names over the Tickets of the Run, up to `max_parallel` at once and under the Merge window.
A Run graph has one, its start reaches it, and `describe` and `open_pr` come after it; its outcomes are `all_merged`, `some_merged` and `none_merged`.
The shipped Run graphs take the Ticket Pipeline from `${vars.pipeline}`, which the project file's `pipeline:` sets.

**Stage**:
A step of a Pipeline in which a Fresh agent with its own role, prompt, model and effort works on the Ticket.
_Avoid_: Step, phase

**Role**:
The reusable job of a Fresh agent, with its prompt, the schema of its output and its skills; several Stages and Deciders may share one Role.
A Role, like its prompt, schema and skills, is found in the Library by name.
_Avoid_: Persona, agent type

**Library**:
The named Roles, prompts, schemas, skills, Pipelines and Run graphs the engine can find, stacked in three Layers.
A Run takes its items from the Library once at its start and does not see later edits.
_Avoid_: Registry, catalog, store

**Layer**:
One source of the Library: `factory` ships with the engine, `user` sits in the factory's home and serves all of the owner's projects, and `project` sits in the Target project.
A name is looked up from `project` down to `factory`, and an item of an upper Layer replaces or extends the item of the same name below it.
_Avoid_: Scope, tier, level

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
An Attempt whose agent the Sandbox's OOM killer killed fails with `oom`, which the Sandbox tells from the kernel's own count and not from the exit code 137, because the engine's SIGKILL on `timeout` gives that code too.
`oom` is an infrastructure failure that spends no `attempts` and no Round, the journal keeps the Sandbox's memory limit and peak usage with it, and the CLI advises raising `sandbox.memory` in the project config.
An Attempt's end writes its duration and its usage (cost, turns and tokens), failed Attempts included; an Attempt that gave no `result` writes its usage as unknown, and a sum over such Attempts shows `+?` after the cost.
The cost is the API price equivalent, which on a subscription is not money charged.
It is always set: the Executor takes it from the agent or prices the tokens by a table of its own, and the usage's `cost_source`, `reported` or `priced`, says which, as `factory stats` shows.
The tokens mean the same for every Executor: the input tokens leave out the cache reads, and the thinking tokens are unknown where the agent does not tell them.
Every node of a Ticket writes its duration, and only Stages, where an agent works, have a cost.
An Attempt's start writes its variant fingerprint: `prompt_sha`, `schema_sha`, `pipeline_sha`, `kit_sha`, the `model` and `effort` it started with after the Stage's `escalate`, and `factory_version`; a Run's start writes `pipeline_sha`, `kit_sha` and `factory_version` for the Run.
The Executor writes the Attempt events, the factory's own record of what the agent did, next to the agent's raw stream, and stall detection and the owner's terminal read only them (ADR 0013).
Among them are the card facts, the prompt, the tool with its input, the tool's output, the last reply, the session id, the subagents and the end of a turn, in the words of no vendor, which the renderer hands to the card of a Workbench that shows one per Attempt.
An Attempt's end writes the `AttemptStats` that the Executor folds out of the agent's stream, such as the requests (unique `message.id`, subagents included), tokens by model, tool calls and the subscription windows' utilization; a field the stream lacks is unknown, not zero.
`factory stats` derives the metrics of Runs, Stages and Tickets from the Journal's events with one pure function and, with `--by`, compares variants by median and p90.
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
A Run has Decisions of its own, `budget_spent` of the budget below, `ci_failed` of the CI wait, and `infra_failure` with the outcomes `retry` and `abort`, which opens when Attempts of two different Tickets fail in a row with the same cause class of `auth`, `sandbox_start`, `executor_crash`, `timeout`, `stalled` or `oom`, or when two Attempts of one Ticket fail in a row with `oom`; `factory decide <run> @run <outcome>` answers it, `retry` spends no Round or attempt, and `abort` ends the Run as `aborted`.
`ci_failed` has the outcomes `recheck` and `accept` and opens when the CI of the Run's final PR is red or does not end in time; `recheck` waits for the CI of the PR's current head again, and `accept` ends the Run `done` with the red CI recorded.
_Avoid_: Judgment, choice, branch

**Operation**:
A deterministic point in a Pipeline where the Orchestrator itself changes a Workspace, the Run's branches or the Tracker, such as `catch_up`, which brings the Integration branch's tip into a Ticket branch so the Gates after it check the combined tree, `merge`, which merges a Ticket into the Integration branch, or `file_issues`, which files the issues a Stage reported.
No agent works in an Operation; a Gate only checks and never changes anything.
_Avoid_: Action, step, hook

**Tracker Operation**:
An Operation `assign`, `label` or `comment`, which acts on the Tracker through its TicketBoard: in a Pipeline on the Ticket, and as a Run node on each Ticket the Run took, or on the Spec with `target: spec`.
It journals the result of each action per target, so `factory resume` does only the actions the Journal lacks.
`on_fail` says what a failure of the Tracker does: `warn`, the default, journals it, shows it in the terminal and goes on, `retry` tries again after a wait with no limit on the tries, and `escalate` ends the node `failed`.
No shipped Run graph has one, so a Run writes to the Tracker only what the project's own graph or Pipeline asks of it, besides the projection.

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
Ctrl-C and `kill` leave the Run orphaned at once, and so does a failed call to a host tool such as `git`, `gh`, `sbx` or `orca`, so the owner can fix the host and resume.
Only `factory resume <run>` takes an orphaned Run up again: it rebuilds the Run from the Journal, stops any agent that outlived the dead Orchestrator instead of adopting it, and loses that Attempt.
`factory finish <run>` ends an orphaned Run instead: it abandons every Ticket that did not merge, keeping its unsaved work under its abandoned ref, ends the Run `partial` and lands the merged Tickets like any `partial` Run, unless the Run was orphaned in phase `ci` or its Run graph has no `open_pr` node.
`factory clean` lists the Workspaces, Sandboxes and Ticket branches that Runs which ended or went stale left behind, and with `--yes` removes them the way the cleanup after `merged` does: a Workspace with uncommitted changes stays with the reason, a branch whose commits are in neither the Integration branch nor the base ref is saved under its abandoned ref first, and a Run that still runs is not touched.

A Run ends only when the CI on its final PR is green or the owner decides otherwise (ADR 0011).
After `PullRequestOpened` the Run is in phase `ci`: it polls its PullRequests for the checks of the pushed head commit, holds no Slot, and keeps beating.
A PR with no check on that commit within the `grace` period of the project file's `ci` has no CI, and the Run journals `CiAbsent` and ends `done`; `ci: off` and a Run that keeps its PR as a file skip the phase.
A Pipeline may declare `ci_fix`, a Run-level Stage with a Role, `rounds`, `attempts`, `timeout`, `stall` and `gates`, the Gates of the Pipeline that a fix must pass.
On red CI the Run is in phase `ci_fix` before the owner is asked: each Round is a Fresh agent in a Workspace and Sandbox of its own on the Integration branch, holding a Slot like any Attempt.
Its Brief holds the failed jobs with their log, the diff of the Integration branch against `base` and, from the second Round on, the previous Round's handoff and Gate verdicts.
After the fixer, a guard by diff refuses a change of the CI config and a deleted, skipped or xfailed test, and then the Gates in `gates` run in the fixer's Sandbox; a refusal or a failed Gate ends the Round and the next one gets its verdict.
A Round that passes commits on the Integration branch (ADR 0009), pushes it and waits for the CI of the new head, and red CI after the last Round opens `ci_failed` with the failed jobs of every Round.
The final PR's body gets a line per Round from the fixer's `failure` and `change`.

`factory resume` of a Run orphaned in phase `ci` goes on waiting for the same commit, and `factory finish` ends it without waiting and journals the CI as unchecked.

**Command**:
A request to change a Run, such as answering a Decision or cancelling a Ticket, that the Orchestrator accepts or rejects; it comes from the CLI, the Run's terminal or a Cockpit, and never changes the Run directly.
The CLI sends `decide`, `cancel` (abandons a Ticket, so Tickets that depend on it never start), `override` (replaces the outcome of the Ticket's current or last Decision and stops the work after it) and `limits` (changes `max_parallel`, the round limits of Stages, the throttle and the budgets).
A Command sent to a Run whose process died waits in the inbox, and `factory resume` applies it on its first tick; the CLI orphans a Run with a stale heartbeat, says that the Command waits and exits, and that holds for `decide` as for the others.
_Avoid_: Message, signal, action

**Slot**:
The right of one Attempt to use the subscription; an Attempt of a Stage or of a Run-level agent starts only once it holds a slot, and gives it back when it ends.
The factory config caps the slots that all Runs on a machine hold at once.
_Avoid_: Lease, seat, token

**Factory config**:
The file `config.yaml` in the factory's home, which sets what all Runs on a machine share, such as the slot cap.
It may also set any key of the project file as a default for every project on the machine.
_Avoid_: Global config, machine settings

**Run settings**:
Every key of the project file as a Run resolved it at its start from its Sources, a later Source winning:
the built-in default, the Factory config, the project file, and `--set` of the invocation.
`RunStarted` records them, besides the values of `sandbox.env`, and a resumed or finished Run goes on with them whatever the project file says by then.
_Avoid_: Layer, which is a source of the Library; overrides, which change nodes of a Pipeline

**Orchestrator**:
The deterministic control loop that drives a Run and is the only component that changes its state.
_Avoid_: Lead, coordinator, mayor, senior agent

**Cockpit**:
An interactive agent session through which the owner watches and steers a Run by way of the Orchestrator's control interface.
_Avoid_: Control room, console, dashboard

**Workbench**:
The environment where the owner sees a Run's Workspaces and Attempts, such as Orca or a plain terminal.
It creates Workspaces and the terminals Attempts run in; where an agent actually runs is its Sandbox.
It also shows the owner the Run tree and tells them when a Decision waits: Orca on its Run card and a plain terminal with BEL.
Orca also shows each Attempt on the agent card of its pane, which the renderer in that pane fills from the card facts through Orca's hook server.
_Avoid_: Driver, UI, IDE, frontend

**Run tree**:
The Run, its Tickets, their states and the Decisions that wait, in the words of no vendor, which the Run view builds and hands to the Workbench.

**Ports**:
The Executors, Sandbox, Workbench and Clock one Run works through, picked once and carried whole in the Run tools.
A Run has every Executor its Pipeline names, and each node runs on the one its Agent settings name, while all of a Ticket's agents share its Sandbox.
_Avoid_: Adapters, services

**Kind**:
One adapter of a port that the project file picks by its name, such as `sbx` of the Sandbox or `gitlab` of the code host, with the fields of its block, the check of its settings and the making of its adapter.
A Tracker kind is picked by the Spec link and a code host kind by the git remotes, unless `code_host` names one.
_Avoid_: Type, flavour, provider

**Port contract**:
The tests in `tests/contracts/` that every Kind of a port passes, each on a fake of its host CLI, which define when a new adapter is done.
A Kind that `wiring` registers without a fake in its port's contract fails the suite.
_Avoid_: Adapter tests, conformance suite

**Run tools**:
What the phases of a Run work with besides the Run's context: the Journal, the project, the Ports and the parts of the Run that keep state the phases share, such as its Attempts, its merge window, its Decisions and its Run agents.
The Orchestrator takes the Run tools whole and hands each phase, besides the Run's context, only the tools it uses and the shared phases it uses, which the Orchestrator builds once per Run, so no phase calls back into the Orchestrator.
_Avoid_: Services, dependencies, toolbox

**Agent settings**:
What a Fresh agent runs with: its `model`, `effort`, `profile`, `attempts`, `timeout`, `stall` and `executor`, which `defaults`, a Stage, a Decider, the Describer and `ci_fix` set.
They travel as one value from the Pipeline file to the Executor, and a Stage's `escalate` gives the settings of its later Rounds.
The `executor` is the project file's unless the block names another, which then also gives the default `model`.
The Journal keeps the `executor`, `model` and `effort` of an Attempt as flat fields of its fingerprint.
_Avoid_: Node config, agent options

**Journal handler**:
The function that tells one reader of the Journal what one event does to its memory, taking `(memory, payload)` and nothing else.
The writer of the event registers it, next to the code that appends the event, in one of the registries of `events`: `applies` for the Run state, `times` for the timeline and `measures` for `factory stats`.
A registry takes one handler per event and fails at import on a second one, so two writers cannot both say what one event does.
_Avoid_: Reducer, listener

### Agents

**Fresh agent**:
An agent started with empty context that receives only pointers to the Ticket and its artifacts.
_Avoid_: Clean agent, new session

**Brief**:
The fixed list of pointers the Orchestrator hands a Fresh agent at the start of an Attempt, holding only the inputs its Stage declares, and a Tracker input by value.
_Avoid_: Context, handoff, prompt

**Artifact**:
A file a Run keeps for its Tickets, such as the output of an Attempt, a Gate verdict, a Decision outcome or the rendered Ticket itself; it never changes once written.
_Avoid_: Output, deliverable, result

**Agent kit**:
The skills, instructions and MCP servers the factory gives every Fresh agent in place of the owner's own agent config.
It is in no Executor's format, and each Executor of the Run translates it with the skills of the Run's Library into a kit of its own, as Claude Code into a plugin with its settings, hooks and MCP config.
_Avoid_: Toolkit, bundle

**Describer**:
The Fresh agent that runs once after the last Ticket merged and writes the title and body of the Run's final PR.
Its Role names the headings and words the body must hold, and the engine checks only those.
The factory Layer's `describer` writes from the skill `pr` and requires `Summary`, `Evidence` and `Merge Danger` with its `Door` and `Blast Radius`.
It reads the Integration branch against the base ref in a Workspace of its own and has no access to the Tracker.
The engine appends the closing of the merged Tickets and the list of the rest to its body, and a Describer that fails leaves the PR with the engine's own text.
It is the Run Stage `describe` of the Run graph, a project that names a Run graph without it opens its PR with the Tracker's text, and `factory stats` shows its cost, which the budget does not count.
_Avoid_: Summarizer, PR bot

**Planner**:
The Fresh agent of the Run Stage `select` that says which Tickets the Run takes, in what order, and why it leaves out each of the rest.
The engine refuses an output that names a Ticket outside those read, names one twice, leaves one unnamed or orders a Ticket before one it depends on, and the Stage retries with the reason in the `cause` of the next Brief, as for a broken schema.
Its `attempts` are the Stage's, and an Attempt whose Sandbox failed counts against `sandbox_retries` instead.
Its output is journaled, so `factory resume` never asks it again, and a Run killed while it works loses that Attempt.
A Planner that fails every Attempt ends `failed` with no Ticket started.
Like the Describer's, its cost shows in `factory stats` and the budget does not count it.
The Run graph `planned` of the Library holds it.

**Run-level agent**:
A Fresh agent that no Ticket owns and that works for the Run in a Workspace and Sandbox of its own, on a branch at the Integration branch's tip, or at the Run's base commit for the Planner: the Describer, the CI fixer of `ci_fix` and the Planner.
Each is known by its name, holds a Slot under that name, journals events of its own and keeps its Artifacts in a directory of the Run under that name; the code calls it `RunAgent`.
_Avoid_: Global agent

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

The throttle is an optional brake from the project file's `throttle`, set per subscription window with a `cap` and a `pace`, and keyed `<executor>.<window>`, such as `claude-code.five_hour`.
The bare `five_hour` and `seven_day` are the windows of the project's `executor`.
Each Executor names the windows it may report, `factory check` warns of a brake on a window its Executor never reports and of a brake or a Budget on an Executor that no node runs on, and an agent on an API key reports none.
The Executor port reports the usage per window with the window's length, which may differ per plan, and while a window is above its `cap` or ahead of its `pace` the Run starts no new Attempts and no agent Decisions on that window's Executor, while the nodes on its other Executors go on.
Running Attempts go to the end, waiting spends no Rounds or attempts, and the Journal records `ThrottleOn` and `ThrottleOff`.

**Budget**:
A cap in dollars from the project file's `throttle`: `budget_usd` for the cost of the whole Run and `<executor>.budget_usd` for one Executor's share of it.
What counts is the cost of the Attempts that ended, so a running Attempt may carry the Run past the cap, a cost right at the cap has spent the budget, and the cost of the Describer and the Planner does not count.
While a budget is spent the Run starts no new Attempts on any Executor, and once one waits it opens the Run's Decision `budget_spent`.
`factory limits --budget-usd` raises the budget, which closes the Decision and lets the Run go on, and the outcome `finish` stops the work, abandons the Tickets that did not merge and lands what merged.
