# Reference: software factories built on AI agents and agent pipelines in sandboxes

Compiled by an agent (Claude Code) at the owner's request on 2026-10-19.
Method: three parallel web research passes (Anthropic, OpenAI, other practitioners and tools) over primary sources.
Some pages were read through the fetch tool's paraphrase, so verbatim quotes should be checked against the original at the link.
The mark **[inference]** means the agent's own judgment; everything else is taken from the sources.
The mark **[secondary]** means the primary source was not found or not read.

## 1. The owner's original question

The goal is to understand how others (above all Anthropic and OpenAI) build pipelines of agents that carry out tasks on their own.
The owner's target scheme:

1. An executor agent does the task.
2. Another agent with an empty context, possibly on a different model, does code review and fixes.
3. After the fixes, yet another agent re-checks.
4. No more than three review cycles, otherwise a human is called in.
5. Different kinds of review (architecture, security and so on) and a QA agent.
6. Everything is chained together, runs in sandboxes and works on several tasks in parallel.

## 2. Short answer

- Almost all major players separate the author agent from the reviewer agent and give the reviewer its own context.
- Inside the company, Anthropic uses a limit of 3 review rounds, after which the reviewer asks the author to reconsider the approach (post from 2026-10-15).
- OpenAI has no hard round limit for review. Convergence comes from a severity threshold (the reviewer raises nothing below P2) and the author's right to push back.
- No ready-made public vendor pipeline has exactly the shape "3 cycles, then a human". The closest are Anthropic's Code Review practice, Managed Agents Outcomes (`max_iterations` defaults to 3) and the open-source skill Superpowers (5 rounds with a model switch).
- Neither Anthropic nor OpenAI promotes review by a different model as a principle. OpenAI states plainly that the Codex generator and reviewer are one model with different training. Factory.ai, Amp and some individual practitioners use a different model for validation.
- The empirical results for cross-model review are asymmetric: one pair of models gains, the reverse pair loses (arXiv 2607.21656).
- Parallelism is in question in 2026: Cognition and Factory write that parallel writers work worse than sequential writing with parallel read-only agents.
- Isolation is built in three layers: a git worktree per task locally, a container or VM per task in the cloud, and credentials and network through a proxy outside the sandbox.

## 3. Mapping to the owner's requirements

| Requirement | Anthropic | OpenAI | Others |
|---|---|---|---|
| Reviewer with a fresh context | Yes, explicitly ("fresh context", separate context windows) | Yes, partly ("disconnected codex", Detached review, `/review`) | Cognition, Superpowers, Factory, Zack Proser |
| A different model for review | As an option (`model` on subagents, `/goal` on a Haiku-class model, advisor) | As an option (`review_model`), but its own review is done by the same model | Factory (GPT-5.3-Codex validates Claude), Amp oracle, Proser (Codex reviews Claude) |
| Limit of N cycles | 3 rounds inside Anthropic; `max_iterations` 3 in Managed Agents | None for review; there is `max_turns`, the auto-review circuit breaker | Superpowers 5; community skills 3 |
| Escalation to a human | By risk class; "step back" after 3 rounds | "Escalate to a human only when judgment is required"; Human Review status | Gas Town `gt escalate`; Superpowers pause; BCG stage gates |
| Specialized reviewers | Bug, security, parallel finders plus voting | Security review, security/tests/maintainability subagents | Every: 15 roles, including architecture-strategist |
| QA agent | Evaluator through Playwright (Labs), e2e skills | The same agent through Chrome DevTools, video | StrongDM simulated testers, Factory validators |
| Parallelism | Agent teams, workflows, `/batch`, cloud sessions | Codex cloud, Codex app, Symphony | Gas Town, Cursor 2.0, Devin manages Devins |
| Sandbox | bubblewrap/seatbelt, VM with an egress allowlist | Seatbelt, bubblewrap+seccomp, a container per task | Docker Sandboxes, E2B, Modal, Daytona, container-use, Sculptor |

## 4. Anthropic

### 4.1. "How the Claude Code team does code review" (2026-10-15)

- Link: https://claude.dev/blog/solving-code-review-at-anthropic/
- Authors: Addy Osmani, Morgan Westlee Lunt, Qing Wang.
- This is the source closest to the owner's request.
- Goal: "the minimum amount of human review time that doesn't raise reverts or incidents".
- Claude writes about 90% of the code of Claude Code.
- From June to September the number of merged PRs more than doubled, and the share of reverts fell by more than 40%.

Seven layers:

1. Verification before the PR. The agent runs tests, the build or screenshots and must "show the output instead of saying it's done". Then a subagent reviews the diff "with fresh context".
2. AI review of every PR with Quick, Standard and Deep levels. "Several agents look for bugs at the same time. Then a separate group votes on each finding, and it only gets posted if enough of them agree it's real."
3. Blocking security review. A PR does not merge while any bug or security review finding is open.
4. Mandatory CI. The PR template asks how it was tested and how to roll it back.
5. Human approval by risk level, which a classifier sets from the change: Low - 0 people, Medium - 1, High - 2. Billing, the auto-updater and CI workflow files always require a human.
6. Merge queue, feature flags, the latest channel before stable.
7. Detection and rollback through flags without a deploy.

Convergence rules:

- "After three rounds, the reviewer asks the author to step back and reconsider the approach."
- After the first passes the reviewer looks only at the new diff and does not repeat findings already posted.
- If a class of bugs is found, the author fixes every instance.
- Counterexample: a PR that went through "more than fifty rounds".
- "An agent's approval never counts as a person's."
- Past incidents became an eval set for the reviewer, which runs daily.

Convergence metrics:

| Metric | Before | After |
|---|---|---|
| Pass on the first round | 65% | 78% |
| PRs with 5+ rounds | 8.9% | 1.5% |
| Average number of rounds | 2.08 | 1.32 |

- Acknowledged gap: "We don't yet have circuit breakers that tighten approval when those numbers move."
- The author and reviewer models are not named.

### 4.2. "How Anthropic secures its AI-native SDLC" (2026-07-21)

- Link: https://claude.com/blog/how-anthropic-secures-its-ai-native-software-development-lifecycle
- Author: Jason Clinton, Deputy CISO.
- "Claude authors about 80% of the code merged into our codebase today."
- Every PR is reviewed by several narrowly focused agents with RAG over past incidents.
- Agents with "separate context windows" do not share blind spots and must prove that a finding is real.
- Human review is kept for "regulated or truly critical code", plus spot checks of agent approvals by risk.
- New AI reviewers first run in shadow mode.
- Development happens on remote VMs, "Agent traffic on these VMs is egress-allowlisted", the "Principle of Least Agency".
- All agent actions go to the SIEM.

### 4.3. The Code Review and Ultrareview products

- Launch blog: https://claude.com/blog/code-review (2026-03-09). Documentation: https://code.claude.com/docs/en/code-review
- Stages Find, Verify, Rank, Post. Agents look for bugs in parallel, then verify them to cut false positives.
- Inside Anthropic the share of PRs with substantive comments grew from 16% to 54%. Less than 1% of findings are marked as wrong.
- About 20 minutes and $15-25 per review.
- "It won't approve PRs - that's still a human call."
- It runs read-only and does not build or run the code. Rules come from `REVIEW.md` on the base branch.
- Ultrareview (`/code-review ultra`): "a fleet of reviewer agents in a cloud sandbox", each finding is reproduced independently. https://code.claude.com/docs/en/ultrareview

### 4.4. Engineering blog: harnesses for long tasks

- "Harness design for long-running application development", Prithvi Rajasekaran, 2026-03-24. https://www.anthropic.com/engineering/harness-design-long-running-apps
  - A planner, generator, evaluator scheme. The evaluator clicks through the live application with Playwright and scores it against criteria with thresholds.
  - "Separating the agent doing the work from the agent judging it proves to be a strong lever."
  - Sprint contracts: the generator and the evaluator agree on the done criteria in advance.
  - 5-15 iterations per frontend generation. A full run takes about 4-6 hours and $125-200 against 20 minutes and $9 for a single agent.
- "Effective harnesses for long-running agents", Justin Young, 2025-11-26. https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
  - An initializer creates a list of 200+ features (all failing), a progress file, `init.sh`. The coding agent takes one feature per session.
  - "It is unacceptable to remove or edit tests."
  - Separate QA and cleanup agents are named as future work.
- "Building a C compiler", Nicholas Carlini, 2026-02-05. https://www.anthropic.com/engineering/building-c-compiler
  - 16 Claudes in parallel, each in its own Docker container, with a shared bare repository.
  - A `while true` loop with a new session every time, tasks are claimed with lock files, there is no orchestrator.
  - Specialized roles: deduplication, performance, design review, documentation.
  - GCC as the oracle. About 2000 sessions, about $20k, 100 thousand lines, it builds Linux 6.9.
- "Building effective agents", Schluntz and Zhang, 2024-12-19. https://www.anthropic.com/research/building-effective-agents
  - The parallelization (voting) and evaluator-optimizer patterns.
  - "common to include stopping conditions (such as a maximum number of iterations)".
  - Agents "pause for human feedback at checkpoints or when encountering blockers".
- "How we built our multi-agent research system", 2025-06-13. https://www.anthropic.com/engineering/multi-agent-research-system
  - An Opus lead, Sonnet subagents, 3-5 in parallel. About 15 times more tokens than in chat.
- "Building multi-agent systems: when and how", Cara Phillips, 2026-01-23. https://claude.com/blog/building-multi-agent-systems-when-and-how-to-use-them
  - Verification subagent: "Verification requires minimal context transfer". The main risk for a verifier is "early victory".
  - Caveat: splitting by type of work (planner, implementer, tester, reviewer) creates a "telephone game". They recommend splitting along context boundaries.
  - Multi-agent systems spend 3-10 times more tokens.
- "Claude Code sandboxing", 2025-10-20. https://www.anthropic.com/engineering/claude-code-sandboxing
  - FS and network isolation through bubblewrap (Linux) and seatbelt (macOS), network through a proxy outside.
  - In the cloud a git proxy injects the token from outside and allows push only to the right branch.
  - Permission prompts dropped by 84%.

### 4.5. Claude Code primitives for pipelines

- Best practices (https://code.claude.com/docs/en/best-practices):
  - Writer/Reviewer: "A fresh context improves code review since Claude won't be biased toward code it just wrote."
  - Adversarial review: a subagent "sees only the diff and the criteria", findings go to the author, then a re-review.
  - Warning: a reviewer asked to look for gaps will always find them. Flag only what affects correctness.
  - If you have corrected Claude more than twice on the same issue, run `/clear`.
  - `/batch` spreads work across 5-30 subagents, each in its own worktree.
- `/goal` (https://code.claude.com/docs/en/goal): after every turn a separate fast model decides whether the goal is reached. "completion is decided by a fresh model rather than the one doing the work."
- Stop hook: the block is lifted after 8 blocks in a row. https://code.claude.com/docs/en/hooks-guide
- Dynamic workflows (https://code.claude.com/docs/en/workflows): a JS script with `agent()`, `pipeline()`, `parallel()`, and the model is set per stage.
  - Example loop condition: "until the type check passes or two rounds in a row make no progress".
  - 16 parallel agents by default, up to 256. There is no user input in the middle of a run; for approval between stages they advise separate workflows.
- Agent teams (https://code.claude.com/docs/en/agent-teams, experimental): lead, teammates, a shared task list, a mailbox. An example of parallel review by three teammates (security, performance, test coverage). `TaskCompleted` hooks as a quality gate.
- Subagents (https://code.claude.com/docs/en/sub-agents): the fields `model`, `isolation: worktree`, `maxTurns`, `background`.
- Agent view (`claude agents`, `claude --bg`): each background session in its own worktree, a notification when an agent needs input.
- Cloud sessions (https://code.claude.com/docs/en/claude-code-on-the-web): a VM per session, an egress allowlist, credentials outside the sandbox.
- Projects (public beta): Claude itself launches parallel cloud threads, one per task with its own branch and PR, with the statuses "Waiting on you" and "Ready for review".

### 4.6. Claude Managed Agents (API)

- Public beta since 2026-04-08, Outcomes and multiagent since 2026-05-06.
- Outcomes (https://platform.claude.com/docs/en/managed-agents/define-outcomes):
  - The grader "uses a separate context window to avoid being influenced by the main agent's implementation choices".
  - It grades against a markdown rubric.
  - `max_iterations` defaults to 3, with a maximum of 20.
  - Results: `satisfied`, `needs_revision`, `max_iterations_reached`, `failed`, `interrupted`. After `max_iterations_reached` control passes to the client.
  - **[inference]** This is a ready-made primitive for "3 cycles, then the outside world decides", exactly fitting the owner's scheme.
- Multiagent (https://platform.claude.com/docs/en/managed-agents/multiagent-orchestration):
  - Agents share one sandbox, but each has an isolated context. Delegation goes one level deep, up to 25 child threads.
  - In the example the lead is on Opus, and the reviewer and test-writer are on Haiku.
  - Advisor - a stronger model for "reviewing work before finishing".

### 4.7. Anthropic people

- Boris Cherny (InfoQ, January 2026, https://infoq.com/news/2026/01/claude-code-creator-workflow/):
  - About 5 local sessions in separate checkouts plus 5-10 on the web.
  - He abandons 10-20% of sessions.
  - Skipping permissions only in a sandbox.
  - Verification improves quality 2-3 times.
  - In a team thread: one Claude writes a plan, a second reviews it "as a staff engineer".
- Boris Cherny and Cat Wu, a video for the Claude Code anniversary, as retold by The Neuron on 2026-06-09 **[secondary]**: "armies of agents" from a few to thousands, routines pick up tickets on their own.

## 5. OpenAI

### 5.1. "Harness engineering" (Ryan Lopopolo, 2026-02-11)

- Link: https://openai.com/index/harness-engineering/
- An internal product with "0 lines of manually-written code": about 1 million lines and 1500 PRs in 5 months, 3-7 engineers.
- The motto "Humans steer. Agents execute."
- Review loop: "we instruct Codex to review its own changes locally, request additional specific agent reviews both locally and in the cloud, respond to any human or agent given feedback, and iterate in a loop until all agent reviewers are satisfied (effectively this is a Ralph Wiggum Loop)."
- "Humans may review pull requests, but aren't required to."
- There is no cycle limit; the loop exits when all reviewers are satisfied.
- Isolation: the application is brought up for each git worktree, an ephemeral stack of logs, metrics and traces per worktree, Chrome DevTools Protocol for checking the UI.
- AGENTS.md of about 100 lines as a table of contents, knowledge in `docs/`. A doc-gardening agent fixes the docs.
- Strict architectural layers are enforced by custom linters, and linter messages contain an instruction for the agent.
- Background Codex tasks look for deviations from the "golden principles" and open refactoring PRs that "can be reviewed in under a minute and automerged".
- "Escalate to a human only when judgment is required."

### 5.2. Lopopolo on the Latent Space podcast (2026-04-07)

- Link: https://latent.space/p/harness-eng
- "Most of the human review is post merge at this point." A human cuts the release branch.
- The main convergence problem: "the Codex driving the code author was willing to be bullied by the PR reviewer, which meant you could end up in a situation where things were not converging."
- Solution: reviewers were tuned to "bias toward merging" and to raise nothing above P2, and the author was allowed to "defer or push back" and move remarks to the backlog.
- Spec loop: "spawn a disconnected codex to implement the spec... Spawn another codex... to review the implementation compared to upstream and update the spec... loop over and over Ralph style".
- The inner build loop is kept within a minute.
- The `$land` skill: wait for reviewers and CI, fix flakes, resolve conflicts, go through the merge queue.

### 5.3. Symphony (2026-04-27)

- Post: https://openai.com/index/open-source-codex-orchestration-symphony/ Repository: https://github.com/openai/symphony
- Linear becomes the control plane: "Every open task gets an agent, agents run continuously, and humans review the results."
- Some teams saw the number of landed PRs grow by 500% in 3 weeks.
- The agent hands in a "review packet": CI, review feedback, complexity analysis, a video walkthrough.
- "treating agents as rigid nodes in a state machine doesn't work well", the agent is given goals rather than rigid transitions.
- SPEC.md: a separate workspace per task, `max_concurrent_agents` defaults to 10, retries with backoff, `turn_timeout` 1 hour, `stall_timeout` 5 minutes. A strong sandbox is listed under Non-Goals.
- OpenAI's WORKFLOW.md:
  - `approval_policy: never`, `thread_sandbox: workspace-write`, `max_turns: 20`.
  - States Todo, In Progress, Human Review, Merging, Done, Rework.
  - Every remark from a human or a bot blocks until it is fixed or answered with "explicit, justified pushback". The loop repeats "until there are no outstanding actionable comments".
  - Rework means "a full approach reset": close the PR, a new branch from main.
  - If access or tools are missing, the task goes to Human Review with a description of the blocker.

### 5.4. Codex cloud, CLI and app

- "Introducing Codex", 2025-05-16 (https://openai.com/index/introducing-codex/): many tasks in parallel, each in its own cloud container with the repository.
- Environments (https://developers.openai.com/codex/cloud/environments): a setup script with internet access, the agent has internet off by default, "secrets are removed before the agent phase starts", traffic goes through a proxy.
- Best-of-N: `codex cloud exec --attempts` from 1 to 4.
- CLI sandbox (https://developers.openai.com/codex/sandboxing): Seatbelt on macOS, bubblewrap+seccomp on Linux (Landlock is deprecated), native on Windows. Modes read-only, workspace-write, danger-full-access.
- Auto-review (https://developers.openai.com/codex/sandboxing/auto-review): a separate agent approves actions at the sandbox boundary and sees a compressed transcript without the hidden reasoning. A circuit breaker interrupts the turn "after 3 consecutive denials or 10 denials within a rolling window of the last 50 reviews".
- `codex exec` for CI: JSONL events, `--output-schema`, danger-full-access only in an isolated runner.
- Codex app (February 2026, https://openai.com/index/introducing-the-codex-app/): many agents in parallel, built-in worktrees, scheduled automations with a review queue.
- Subagents (https://developers.openai.com/codex/subagents): the example "Spawn one subagent for security risks, one for test gaps, and one for maintainability", their own models in `.codex/agents/*.toml`. Subagents inherit the parent's sandbox; there is no separate read-only sandbox for the reviewer.

### 5.5. Codex code review

- "Introducing upgrades to Codex", 2025-09-15 (https://openai.com/index/introducing-upgrades-to-codex/): the model is trained for review and runs code and tests. "At OpenAI, Codex now reviews the vast majority of our PRs." The stance: an additional reviewer, not a replacement for a human.
- "A Practical Approach to Verifying Code at Scale", 2025-12-01 (https://alignment.openai.com/scaling-code-verification/):
  - "The Codex 'code generator' and 'code reviewer' are the same model. But the training methods... differ."
  - Precision is put above recall. Access to the repository and to code execution improves both.
  - Reviewer comments get a fix in response in 52.7% of cases.
  - "Verification can be cheaper than generation."
- Current docs (https://developers.openai.com/codex/code-review): only P0 and P1 in GitHub, rules in the AGENTS.md section "## Code Review Rules", a separate `@codex security review`, the `review_model` setting and Detached mode (a separate chat for review).

### 5.6. Agents SDK and the guide

- "A practical guide to building agents" (April 2025, PDF): escalation to a human on "Exceeding failure thresholds: Set limits on agent retries or actions" and on risky actions.
- The `llm_as_a_judge.py` example in openai-agents-python: a generator and judge loop, `max_rounds=3` in auto mode. The judge sees the generator's whole input; there is no fresh context.
- "The next evolution of the Agents SDK", 2026-04-15 (https://openai.com/index/the-next-evolution-of-the-agents-sdk/): native sandboxes (Daytona, E2B, Modal, Cloudflare, Vercel and others), "Separating harness and compute helps keep credentials out of environments where model-generated code executes", subagents in isolated environments in parallel.
- An archived cookbook with Codex and the Agents SDK: PM, Designer, Frontend and Backend in parallel, Tester. No reviewer and no limit.

### 5.7. OpenAI people

- Thibault Sottiaux (Pragmatic Engineer, 2026-02-17, https://newsletter.pragmaticengineer.com/p/how-codex-is-built): an engineer on the Codex team runs 4-8 parallel agents (features, review, security). Non-critical code is merged after AI review; the agent core requires a human.
- Sora for Android (2025-12-12, https://openai.com/index/shipping-sora-for-android-with-codex/): 4 engineers, 28 days, about 85% of the code from Codex, parallel sessions per subsystem. The bottleneck moved to decisions and integration.
- Alexander Embiricos (Lenny's Podcast, December 2025): the bottleneck moves from writing to review. **[secondary]** for the quotes.
- Greg Brockman **[secondary]**: the share of code from agents grew from 20% to 80% during December 2025, "a human is responsible for all code that is merged".

## 6. Practitioners and companies outside Anthropic and OpenAI

### 6.1. Jesse Vincent, Superpowers (the closest to the owner's scheme)

- Skill subagent-driven-development: https://github.com/obra/superpowers/blob/main/skills/subagent-driven-development/SKILL.md
- Each task gets a new implementer subagent in an isolated worktree. Subagents "should never inherit your session's context or history".
- One task reviewer per task gives two verdicts: conformance to the spec and quality.
- "Five rounds maximum per task". Rounds 1-3 go to the original implementer, rounds 4-5 to a new implementer on a stronger model, and in round 5 the controller decides on each open finding.
- The final review of the whole branch is done by the strongest model.
- Pause for a human: irreversible operations, security, push/merge, a broken plan.
- Implementers are not run in parallel.

### 6.2. Cognition (Devin)

- "Devin Review", 2026-01-21 (https://cognition.com/blog/devin-review): "code review - not code generation - is now the bottleneck".
- "Devin Autofixes Review Comments", 2026-02-10: "One agent writes, the other pressure-tests, and this continues in a loop." Mechanics without a human, architecture stays with people. No limit.
- "Devin can now Manage Devins", 2026-03-19 (https://cognition.com/blog/devin-can-now-manage-devins): each child Devin in its own VM with a clean context, a coordinator merges the results.
- Walden Yan, "Multi-Agents: What's Actually Working", 2026-04-22 (https://cognition.com/blog/multi-agents-working):
  - "we found this technique to work best when the coding and review agents do not share any context beforehand." The reviewer sees only the diff, without the spec.
  - Review finds about 2 bugs per PR, about 58% of them serious. Often several cycles, each finding something new.
  - The author filters findings by the user's instructions so as not to loop forever.
  - "writes stay single-threaded": parallel writers did not take hold, only read-only subagents work in parallel.
  - "Smart Friend": the main model calls another frontier model as a tool.

### 6.3. Factory.ai Missions

- https://factory.com/news/missions (the page is dated 2025-02-26, but judging by the models mentioned it is 2026).
- The orchestrator (Opus) plans milestones, workers (Sonnet/Opus) get "a fresh worker session with clean context" for each feature.
- Validators on GPT-5.3-Codex run the tests and click through the application themselves. The validator is from a different model family than the executor.
- "Every milestone ends with a validation phase." A human approves the plan.
- "Serial execution with targeted parallelization has worked better than broad parallelism."

### 6.4. Every, Compound Engineering (Kieran Klaassen, Dan Shipper)

- Article from 2026-01-30: https://every.to/source-code/compound-engineering-how-every-codes-with-agents-af3a1bae-cf9b-458e-8048-c6b4ba860e62
- The loop Plan, Work, Review, Compound. The last step records lessons so the next iteration is easier.
- `/ce:review` runs up to 15 reviewers in parallel: architecture-strategist, security-sentinel, performance-oracle, code-simplicity-reviewer, data-integrity-guardian, data-migration-expert, deployment-verification-agent, schema-drift-detector and personas such as dhh-rails-reviewer.
- A human decides what to fix. No cycle limit.

### 6.5. StrongDM Software Factory

- https://factory.strongdm.ai/ (2026-02-06) and Simon Willison's write-up https://simonwillison.net/2026/Feb/7/software-factory/
- Rules: "Code must not be written by humans." and "Code must not be reviewed by humans."
- Benchmark: at least $1000 of tokens per day per engineer.
- Scenarios - end-to-end scenarios stored outside the codebase as a holdout set, so the agent does not fit the code to them.
- A "satisfaction" metric instead of green tests, because tests are open to reward hacking.
- Digital Twin Universe: agents build behavioral clones of Okta, Jira, Slack, Google Docs and run thousands of scenarios an hour against them.
- **[inference]** Holdout scenarios play the role of independent QA with a fresh eye, but at the level of behavior, not code.

### 6.6. Steve Yegge, Gas Town and Beads

- Gas Town (2026-01-01, https://github.com/steveyegge/gastown, heise overview https://heise.de/-11178824):
  - The Mayor coordinates, Polecats write code, the Witness watches for stuck agents, the Deacon escalates, the Refinery runs the merge queue, and the human plays the Overseer.
  - The Refinery batches MRs, runs verification gates on the merged stack, and on a failure bisects to the culprit. Agents do not push to main themselves.
  - There is no separate code review stage; there are review presets and hourly review sweeps that file tasks.
  - Isolation through git worktrees. Escalation `gt escalate` with levels P0-P2, the route Deacon, Mayor, Overseer.
  - There is no retry limit; `max_polecats` limits only parallelism.
- Beads (https://github.com/steveyegge/beads): an issue tracker for agents, `bd ready` and an atomic `--claim` to coordinate parallel agents.
- "Revenge of the junior developer" (2025-03-22): a forecast of fleets of 100+ agents under supervisor agents that call a human when an agent is stuck.

### 6.7. Geoffrey Huntley, Ralph loop

- https://ghuntley.com/ralph/ (2025-07-14): `while :; do cat PROMPT.md | claude-code ; done`.
- One item per loop, each pass with a clean context, quality through backpressure (build, tests, type checker) rather than through a reviewer agent.
- Fits greenfield work. OpenAI calls its review loop "effectively a Ralph Wiggum Loop".

### 6.8. Cursor

- Cursor 2.0, 2025-10-29 (https://cursor.com/blog/2-0): many agents in parallel in worktrees or on remote machines. Several models solve one task, and the best result is picked.
- "Building a better Bugbot", 2026-01-15 (https://cursor.com/blog/building-bugbot): first 8 parallel passes with a shuffled diff order and majority voting, then a validator against false positives. Later a move to an agentic design gave the biggest gain.
- More than 70% of Bugbot findings are fixed before merge.

### 6.9. GitHub Copilot coding agent and Agent HQ

- The agent "reviews its own changes using Copilot code review before it opens the pull request", and CodeQL and secret scanning run in the loop (changelog 2025-10-28).
- The environment is ephemeral in GitHub Actions with a firewall by default.
- Agent HQ: agents from Anthropic, OpenAI, Google, Cognition in one panel; several agents can be put on one task and compared.

### 6.10. Others

- Google Jules, "critic-augmented generation" (2025-08-12): a critic looks at the patch and returns it to Jules, looping "until no further issues remain".
- Amp, Oracle (2025-07-04, https://ampcode.com/news/oracle): a read-only subagent on another vendor's model for review and debugging; because of the cost, the main agent calls it on its own judgment.
- Zack Proser, 2026-06-24 (https://zackproser.com/blog/codex-reviews-claudes-diffs): a Stop hook in Claude Code sends the diff to Codex without the author's transcript, verdict approve/comment/block, only correctness and security block, and a human decides when the models disagree. "the model that wrote the code is the worst possible judge of it".
- Simon Willison, "Embracing the parallel coding agent lifestyle" (2025-10-05): the bottleneck is human review, he can drive one significant change at a time. "Designing agentic loops" (2025-09-30): YOLO only in a sandbox, credentials only for staging with a budget. "The lethal trifecta" (2025-06-16): private data, untrusted content and an outbound channel are dangerous together.
- Peter Steinberger (around October 2025): 3-8 agents in one folder without worktrees, atomic commits, he gives specs to another model for review.
- Mitchell Hashimoto (2026-02-05): deliberately does not run many agents. "If you give an agent a way to verify its work, it more often than not fixes its own mistakes."
- Armin Ronacher (2025-06-12): separate checkouts, Docker for YOLO, separate the shared state (DB, Redis).
- Addy Osmani (2026-01-02): a move from conductor (one agent, synchronously) to orchestrator (asynchronous parallel agents, the result as a PR), with the human as the final failsafe.
- Andrej Karpathy (January 2026, as retold by The Decoder) **[secondary]**: "mostly programming in English now", models make subtle conceptual mistakes, "watch them like a hawk".
- Dan Shapiro, "The Five Levels" (2026-01-23): level 5 "Dark factory" - "a black box that turns specs into software".
- BCG Platinion, "Agentic Software Factory" (2026-03-26): instead of line-by-line review, layered verification (scenario tests from independent agents outside the codebase, architecture conformance, red-team agents), with the human as the accountable approver at stage gates.
- Harper Reed (2025-02-16): spec, then a plan, then execution with tests. Separate prompts for code review and for missing tests.

## 7. Isolation and parallelism tools

| Tool | How it isolates | Notes |
|---|---|---|
| git worktrees | A separate working copy per task | Cursor, Gas Town, Conductor, Claude Code, Codex app, Superpowers |
| Dagger container-use | A container and a branch per agent, an MCP server | Early development |
| Imbue Sculptor | A Docker container per Claude Code agent | Pairing Mode syncs with the IDE |
| Conductor | worktrees, a Mac app | Claude Code and Codex |
| Vibe Kanban | worktrees, kanban, diff review | According to secondary data the company shut down in April 2026 |
| Claude Squad | tmux plus worktrees | Several CLI agents |
| Docker Sandboxes | A microVM with its own Docker daemon | `docker sandbox run claude` |
| E2B | Firecracker microVM | Sessions up to 24 hours on Pro |
| Modal | gVisor, snapshots | Up to 24 hours |
| Daytona | Containers, a VM option | Supported in the OpenAI Agents SDK |

Counterexamples to the worktree approach: Steinberger works in one folder with atomic commits, Willison and Ronacher use separate checkouts, Sculptor and Carlini use containers.

## 8. Review by a different model: the data

- arXiv 2607.21656, "Cross-Model LLM Code Review", 2026-07-22 (https://arxiv.org/abs/2607.21656): 116 LiveCodeBench tasks, a reviewer that does not run tests.
  - Claude reviews Codex: 71.6% became 89.7%. Codex on itself: 84.5%.
  - Codex reviews Claude: 91.4% became 82.8%, that is, worse. Claude on itself: no change.
  - The authors conclude the effect is asymmetric.
- Self-preference bias (Wataoka et al., NeurIPS 2024, https://arxiv.org/abs/2410.21819): models prefer texts with low perplexity for them, that is, their own.
- **[inference]** A fresh context removes the bias toward one's own solution. Switching the model also removes the bias toward one's own style, but the effect depends on the pair of models and has to be measured on your own tasks.

## 9. Cycle limits and escalation: what public sources have

| Where | Limit | What happens after the limit |
|---|---|---|
| Anthropic, internal Claude Code review | 3 rounds | The reviewer asks the author to reconsider the approach |
| Claude Managed Agents Outcomes | `max_iterations` 3 by default, up to 20 | Status `max_iterations_reached`, the client decides |
| Claude Code best practices | 2 corrections | `/clear` and a new prompt |
| Claude Code workflows (example) | 2 rounds without progress | Stop |
| Codex auto-review | 3 denials in a row or 10 out of 50 | The turn is interrupted, the user is asked |
| Symphony WORKFLOW.md | `max_turns` 20 | Human Review |
| OpenAI Agents SDK example | `max_rounds` 3 (auto mode only) | Stop |
| Superpowers | 5 rounds, a stronger model from round 4 | The controller decides, pause for a human |
| Community skills (claudskills, camacho/ai-skills) | 3 rounds, up to 5 | A human or an ESCALATE verdict |
| LangGraph Reflection | `MAX_ITERATIONS` 5 in the example | Stop |
| OpenAI Harness, Cognition, Jules, Every | None | Loop until the reviewers are satisfied |

- Anthropic openly admits that circuit breakers on convergence metrics are not built yet.
- OpenAI handles non-convergence not with a counter but with a severity threshold (the reviewer raises nothing below P2) and the author's right to push back.
- Anthropic handles it with re-review of only the new diff, a ban on repeating findings already posted and a rule to fix the whole class of bugs at once.

## 10. Parallelism: the 2026 debate

- For massive parallelism: Yegge (agent fleets), Cursor 2.0, Carlini (16 Claudes), Symphony (up to 10 agents on tasks from Linear), Cherny (5-15 sessions), Sottiaux (4-8 agents).
- Against parallel writers: Cognition ("writes stay single-threaded"), Factory ("serial execution with targeted parallelization"), Superpowers (implementers in sequence), Hashimoto (one agent).
- Anthropic (Cara Phillips) warns that splitting by role (planner, implementer, tester, reviewer) creates a "telephone game" and advises splitting along context boundaries.
- **[inference]** The sources converge on a compromise. Independent tasks run in parallel in isolated environments, as do read-only reviewers on a single task. Writing within one task is done by one agent.

## 11. Cross-cutting patterns (the agent's inference)

1. The author first verifies the work itself and attaches evidence (tests, screenshots, video). Anthropic, OpenAI, Hashimoto and Factory all have this.
2. The reviewer gets only the diff and the criteria, without the author's history.
3. Several parallel narrowly focused reviewers, then a false positive filter (voting or a verifier).
4. Only serious findings block. Minor things go to the backlog or are marked as nit.
5. Convergence rests on a round limit, review of only the new diff and the author's right to push back.
6. A human joins by the risk class of the change, not on every PR.
7. Knowledge and rules live in the repository (AGENTS.md, REVIEW.md, docs/), and past incidents turn into evals for the reviewer.
8. Isolation: a worktree or container per task, network through an allowlist, credentials outside the sandbox.
9. Independent behavior checks (holdout scenarios, a QA agent with a browser) are valued above yet another code reviewer.

## 12. Gaps

- There is no public vendor document with a pipeline of exactly the shape "executor, reviewer on a different model, fix, re-review, at most 3 cycles, human".
- The author and reviewer models in Anthropic's internal pipeline are not disclosed.
- A separate architecture reviewer in the production pipelines of Anthropic and OpenAI is not described. Anthropic leaves architecture to people. Every and BCG have architecture roles.
- Not read: the original "Welcome to Gas Town" (403), Cognition's post on verification in a VM (404), Osmani's "The Orchestration Tax Is You", the full Embiricos transcript, the primary sources for Brockman and Karpathy.
- The Factory Missions date on the page contradicts the models it mentions.
- The number of Every reviewers differs (12 in the article, 15 in the documentation), probably it grew over time.
