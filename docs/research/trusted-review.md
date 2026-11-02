# Trusted review: how to merge agent-made PRs with confidence and little human time

Date: 2026-11-02.
Sources:
- the post [How the Claude Code team does code review](https://claude.dev/blog/solving-code-review-at-anthropic/) (Anthropic, 2026-10-15);
- the owner's Trusted Review project notes, which hold 33 hypotheses checked against about 120 external sources;
- the factory's own Runs from 2026-10-22 to 2026-11-02, read from `~/.local/share/software-factory/`.

The terms Run, Ticket, Stage, Attempt, Gate, Decision and Journal are used in the sense of `CONTEXT.md`.
Follow-up: Spec #212 takes the cheapest steps of the rollout below.

## Short answer

The factory's review is noisy because it asks an open question ("find what is wrong") against a generic code-smell list.
Every one of 64 recorded reviews found something, with a median of 11 findings, and two thirds of the findings were skipped.
The C++ `characterize` reviewer works from a closed list of nine failure types and produced 178 concrete findings with none left unfixed.
So the first lever is to tell the reviewer what failure looks like in this project, and to treat everything else as non-blocking.

The industry practice that holds up best under external evidence has six parts:
- mergeable is a set of checkable conditions, not a reviewer's opinion;
- a blocking finding needs executable evidence;
- the reviewer is calibrated on the outcome of its findings, not on prompt wording;
- the review loop has a round cap and re-reviews only the new diff;
- a risk matrix set by the change, not the author, decides how deep to check and when to call a person;
- trust grows from measured misses and needless escalations, and misses are measured with planted bugs.

The rollout below starts with a step that changes nothing in the Pipeline.
It makes findings visible in the Journal and fixes a baseline, so every later step is an experiment with a "before" number.

## 1. What the factory's Runs show

The data covers 50 Runs: 41 on the `self` Pipeline and 9 on the C++ `characterize` Pipeline for `mobile/welltorycorecommon`.
Cost is recorded only from 2026-10-29.

### 1.1 `self` Pipeline

- `review` runs once per Ticket, on opus, and finds and fixes in the same Attempt (`.factory/pipelines/self.yaml`, Stage `review`).
- The reviewer skill runs two parallel axes, Standards and Spec, with a Fowler code-smell baseline (`src/factory/kit/plugin/skills/review/SKILL.md`).
- Findings carry `outcome: fixed | skipped` and a reason.
  They carry no severity, no class, no independent check that the finding is real, and no re-review after the fix.

| Measure | Value |
|---|---|
| Reviews with findings | 64 |
| Findings | 744 |
| Findings per review | min 2, median 11, max 20 |
| Fixed | 249 (33%) |
| Skipped | 495 (67%) |
| Skipped on the Standards axis | 65% |
| Review cost since 2026-10-29 | $33.3 over 29 Attempts |
| Implement cost since 2026-10-29 | $22.9 over 31 Attempts |
| Human Decisions in the Journal | 0 |
| Human reviews or comments on factory PRs | 0 |
| Reverts on `main` | 0 |

Facts that matter for the design:
- No review ever reported zero findings.
- The most common skip reasons mention "owner", "judgement", "outside", "existing" and "already".
- Skipped findings are kept only in the Attempt's `output.json` and in the PR text.
- At least one skipped finding was a real bug.
  Ticket t40 skipped "A renderer never stops if the Run process crashes" (`render.py:84`) as outside the Ticket, and issue #191 and PR #193 later fixed exactly that leak.
- About 20 other skipped findings use words such as race, crash, never, lost or wrong, for example t38 (`recovery.py:40`, replay does not rebuild `self.failures`) and t37 (`cli.py:378`, `_send` can wait forever).
- The reviewer's own edits are checked only by the Gates after `sync`, and `fix-synced` ran on 2 Tickets.
- Review costs more than implementation.

### 1.2 `characterize` Pipeline (C++ tests)

- The `assertion-reviewer` is fresh on purpose and knows nothing of how the tests were written.
- It looks for hollow assertions from a closed list of nine problems, fixes them, and re-measures coverage.
- It found 178 problems across 26 reviews, and none was left unfixed.
  The largest classes were unchecked output (71) and shape instead of value (41+).
- Examples are concrete, such as a range check `>= 300` pinned to `== 370`, or a test named for awakenings whose input had none.
- `acceptance` sent 7 of 24 Tickets to rework, and all of them converged on round 2.
- Both abandoned Tickets failed on `file_issues` bookkeeping rather than on code, and PRs #198 and #200 fixed that in the engine.

### 1.3 What already exists but is unused

`src/factory/library/pipelines/tdd-review.yaml` already has a read-only reviewer, a `triage` Decision that drops taste and tooling findings, a `risk` function that routes one-way or wide changes to the owner, and a `ruling` when fix Rounds run out.
No recorded Run has used it.

### 1.4 Telemetry

Recorded today:
- per Attempt, the variant fingerprint (`prompt_sha`, `schema_sha`, `pipeline_sha`, `kit_sha`, `model`), plus cost, tokens, duration and tool calls;
- per Gate, the outcome and failed lines;
- per Ticket in `src/factory/metrics.py`, `rounds`, `fix_rounds`, `first_pass`, merge size and `by_hand`;
- per Decision, the outcome and reasons;
- `factory stats --by`, which compares variants by median and p90.

Missing:
- findings are not Journal events, and `metrics.py` cannot count them;
- findings have no class or severity;
- nothing links a skipped finding to a later issue or bug;
- there is no record of the owner's own verdict on a PR.

## 2. Distilled knowledge

Codes such as [E14] refer to the source table in section 4.

### 2.1 Goal and guardrail

- The Claude Code team optimizes for "the minimum amount of human review time that doesn't raise reverts or incidents".
- Human time per PR is the target metric.
  Misses and reverts are the guardrail.
  If the guardrail rises, human time was not saved, only moved.
- Human time must be measured, not felt.
  In a controlled trial developers believed AI made them 20% faster, while their tasks took 19% longer [E37].

### 2.2 Mergeable is a set of checkable conditions

| Condition | Why |
|---|---|
| The intent of the change is written down (Spec, Ticket) | The reviewer checks the diff against intent, and a person can judge scope |
| Deterministic checks are green and CI is not weakened | Cheap, cannot be argued with |
| Tests were not bent to fit the code | Models cheat mostly by editing tests, over 79% of cheating cases for Claude models [E4] |
| No open blocking finding | Every finding is either fixed or answered |
| The risk tier allows merge without a person | See 2.5 |
| There is a way back | Revert, flag or rollback path |

- Use three verdicts: `mergeable`, `needs-human` with a reason from a closed list, and `not-mergeable`.
- A green CI alone is weak evidence, because 29.6% of SWE-bench Verified patches that pass the tests behave differently from the reference fix [E23].
- An agent's approval never fills a slot that calls for a person.
- Someone is accountable for every merge.
  Under auto-merge that can be the owner of the auto-merge rule rather than a reader of each diff [E1] [E25].

### 2.3 How to cut noise

| Practice | Evidence |
|---|---|
| Three finding classes: blocking, useful non-blocking (goes to a backlog), noise (suppressed) | Most human review value lies in non-blocking maintainability comments [E15], so they should not be lost, only kept out of the merge path |
| A blocking finding needs executable evidence: a failing test, a trace or a reproduction | Asking an LLM reviewer to explain its verdict raised false rejections of correct code from 26.2% to 73.2%. Running the proposed fix against tests cut false rejections from 54.8% to 16.3% [E20] |
| Several independent passes plus an aggregator or vote | Aggregating ten runs of one model raised F1 by 43.67% on 1000 real PRs [E6]. Anthropic posts a finding only if a separate voting group agrees it is real |
| Tag `pre-existing` separately | About 59% of what the Claude Code reviewer kept re-flagging was code that predated the PR |
| A short `REVIEW.md` per repo | Removing review rules was the largest single loss in Atlassian's reviewer, minus 5 points [E12]. Good files say what failure looks like, cap nits, require file and line, and make "0 findings" a normal answer that opens the summary |
| Calibrate on the outcome of findings | Google raised useful comments from 54% to 66% by suppressing 17 rules people marked useless, and to 80% with better descriptions [E13]. Atlassian's filter trained on "did the comment lead to a code change" gave +20 points, while an LLM judge gave almost nothing [E12] |
| Retire a rule that is often useless | Google keeps a check in review only while under 10% of its findings are marked "Not useful" [E14] |
| Treat the author agent's PR description as untrusted | A confident description makes LLM reviewers miss planted vulnerabilities [E24] |

How this maps to the factory:
- The `characterize` closed list is a `REVIEW.md` done right.
- The `self` Fowler baseline is the open question that produces most of the skipped findings.
- In `self`, "fixed" is the reviewer's own choice, so it is not the outcome signal Google and Atlassian calibrate on.
  The factory needs the owner's labels, or a downstream signal such as a later issue or bug, to calibrate.

### 2.4 Loop convergence

- Cap review rounds at three.
  After the cap the reviewer asks the author to reconsider the approach, or the Pipeline hands the decision to a `ruling`.
- After the first pass, re-review only the new diff and never repeat a posted finding.
- When a finding names a class of bug, fix every instance, not one line.
- Accept the author's pushback only with evidence, because models drop a correct answer after a plain "Are you sure?" in 42-98% of cases [E21].
- Late rounds also hurt quality.
  After five rounds of LLM "improvement", critical vulnerabilities rose by 37.6% [E22], and multi-round review quality drops with each round [E47].
- With these rules the Claude Code team moved from 65% to 78% passing after one round, from 8.9% to 1.5% needing five or more, and from 2.08 to 1.32 mean reviews per PR.

### 2.5 Risk matrix

- The change sets the tier, not the author.
- The matrix works in two layers.
  The first layer is static tiers by path, which no agent can talk its way around.
  The second layer is a classifier that only ranks what to look at harder, and is never the only gate [E2] [E30].
- The best-supported predictor of risk is change size.
  Logistic regression on added lines does as well as deep models [E29], so any classifier needs a "size only" baseline.
- Reversibility is the main difference between backend and mobile.
  For mobile software, hot-fixes and rollback are "largely unacceptable" [E27].
- For high risk, depth means more independent behavior checks and a person, not a longer code review.
  Experts who started from an LLM review found more small issues but not more serious ones [E31].
- The matrix should tighten itself when numbers move.
  Google's error budget policy freezes all changes except P0 and security once the four-week budget is spent [E28].
  The trigger, action and release condition are written down in advance.

Candidate axes:

| Axis | Low | High |
|---|---|---|
| Blast radius | Internal tool, one user | Shared library, many users |
| Reversibility | Revert or flag, no deploy | Mobile release through a store |
| Sensitive zone | Docs, copy | Auth, billing, data, CI workflows, release and installers |
| Size and spread | Few lines, one module | Many lines, many modules |
| Touches tests, coverage thresholds or CI | No | Yes, always deterministic check plus a person |
| Source | Owner's Spec | Follow-up cut by a reviewer, external input |

### 2.6 Trust from zero

- Start in shadow mode.
  The owner gives a verdict before seeing the agent's verdict, otherwise the log measures agreement with an anchor [E31] [E32].
- Measure needless escalations as Google measures false positives, where a finding is false if the person did nothing with it [E14].
- Misses cannot be measured on the natural PR flow of one owner, because serious bugs are rare.
  Plant bugs on purpose, take them from the project's own history, and ship them with a confident PR description [E34] [E36] [E24].
- Every miss and every disagreement with the owner becomes an eval case.
  Every change to a prompt, tool or model is scored against the whole set, and a change that does not raise the score is dropped.
- A person is also an unreliable sensor.
  People miss 30% of targets when targets are rare [E34], so planted bugs also go into the owner's audit sample.

## 3. Rollout plan

Each step is a hypothesis with a metric and a threshold.
Variants are compared with the existing `prompt_sha` and `pipeline_sha` fingerprints and `factory stats --by`.

### 3.1 Step 0: make findings visible and fix a baseline

Changes:
- Add a `ReviewFinished` Journal event that counts findings per axis, class and outcome, and marks `pre-existing`.
- Teach `metrics.py` to read it.
- Backfill the 744 existing findings.
- The owner labels a random sample of about 100 findings as `blocking`, `useful` or `noise`.

Baseline:

| Metric | Value on 2026-11-02 |
|---|---|
| Findings per Ticket | 11 (median) |
| Share fixed by the reviewer | 33% |
| Review cost per Ticket | about $1.15 |
| Human Decisions | 0 |
| Known misses | at least 1 (t40, later #191) |

The labeled sample answers two questions before any change: how much of the output is noise, and how many real bugs hide among skipped findings.

### 3.2 Step 1: a new output contract for the reviewer

Changes:
- Each finding carries a class (`blocking`, `useful`, `noise`), a `pre-existing` tag and evidence.
  A blocking finding carries file, line and an executable reproduction or a failing test.
- The Fowler smell baseline moves from the merge path to the non-blocking backlog.
- A short `REVIEW.md` for the factory lists what failure looks like here, in the style of the nine `characterize` types.
  Candidates are a broken Journal or replay guarantee, a hung process or a leaked resource, a lost event, a bypassed Gate, and a test that cannot fail.
- The summary opens with the number of blocking findings, and zero is a normal answer.

Success:
- `noise` share in the owner's labels falls below 10%;
- the share of findings the owner calls `blocking` rises;
- recall on known bugs (t40, t38, t37) does not fall.

### 3.3 Step 2: split find, verify and fix

Changes:
- `review` becomes read-only, as in `tdd-review`.
- A verify step reproduces each blocking finding or puts it to an independent vote, and only survivors block.
- `fix` addresses the survivors.
- Re-review covers only the new diff, with a cap of three Rounds and a `ruling` after the cap.
- Confirmed findings that the Ticket does not fix are filed through the existing `file_issues` kind, which closes the leak that let t40 through.

Success:
- `fix-synced` after review triggers no more often than today;
- mean review Rounds per Ticket stays at or below 1.3;
- the share of skipped findings that later surface as bugs goes to zero.

### 3.4 Step 3: a risk matrix as a project file

Changes:
- Each project has `.factory/risk.yaml`, read by a deterministic `risk` function.
  The first layer is paths, the second is diff features such as size and spread.
- Low merges on green checks.
  Medium returns `needs-human` with a reason.
  High needs a person and an extra behavior check.

First draft per project:

| Axis | Factory (`self`) | welltorycorecommon (C++, mobile) |
|---|---|---|
| Reversibility | High, a local tool | Low, ships in a mobile release |
| Sensitive paths | Journal and DB schema, recovery, merge and git operations, `.factory/pipelines`, role prompts | Production code outside tests, CI |
| Size and spread | Lines and modules touched | Same |
| Tests, thresholds or CI touched | Deterministic check plus a person | `characterize` must not touch production code, checked deterministically |

Success:
- the owner marks each escalation as justified or not, and needless escalations stay under 10%;
- the matrix beats a "diff size only" baseline on the owner's labels.

### 3.5 Step 4: an eval set and planted bugs

Changes:
- Seed cases:
  - t40, the renderer leak;
  - t38, replay that does not rebuild failures;
  - t37, `_send` waiting forever;
  - `characterize` #44, a NaN path wrongly called dead code;
  - hollow assertions from the `characterize` MRs;
  - a few PRs with tests bent to fit the code.
- Run the set on every change to a prompt, model or Pipeline.
  Accept a variant only if recall holds and noise falls.
- Once a week, plant one realistic bug in the live flow.
  It measures both the Pipeline and the owner.

### 3.6 Step 5: shadow mode, then autonomy, then a circuit breaker

Changes:
- On every fifth PR the owner records a verdict before seeing the agent's verdict.
  Disagreements go into the eval set.
- Auto-merge for the Low tier starts on a written threshold, for example 30 PRs in a row with no miss on planted bugs and under 10% needless escalations.
- A circuit breaker raises Low to Medium for four weeks after a revert or a missed planted bug.

## 4. Sources

| Code | Source |
|---|---|
| Post | Anthropic, [How the Claude Code team does code review](https://claude.dev/blog/solving-code-review-at-anthropic/), 2026 |
| E1 | Adams et al. (Meta), [Automating Low-Risk Code Review at Meta: RADAR](https://arxiv.org/abs/2605.30208), 2026 |
| E2 | Abreu et al. (Meta), [Using LLMs in Release Deployment](https://arxiv.org/abs/2410.06351), ICSE SEIP 2025 |
| E4 | Zhong et al., [ImpossibleBench](https://arxiv.org/abs/2510.20270), ICLR 2026 |
| E6 | [SWR-Bench](https://arxiv.org/abs/2509.01494), FSE 2026 |
| E12 | Tantithamthavorn et al. (Atlassian), [RovoDev Code Reviewer](https://arxiv.org/abs/2601.01129), ICSE SEIP 2026 |
| E13 | Vijayvergiya et al. (Google), [AutoCommenter](https://arxiv.org/abs/2405.13565), AIware 2024 |
| E14 | Sadowski et al., [Lessons from Building Static Analysis Tools at Google](https://research.google/pubs/lessons-from-building-static-analysis-tools-at-google/), CACM 2018 |
| E15 | Czerwonka, Greiler, Tilford (Microsoft), [Code Reviews Do Not Find Bugs](https://www.microsoft.com/en-us/research/wp-content/uploads/2015/05/PID3556473.pdf), 2015 |
| E20 | Jin, Chen, [Are LLMs Reliable Code Reviewers?](https://arxiv.org/abs/2603.00539), 2026 |
| E21 | Sharma et al. (Anthropic), [Towards Understanding Sycophancy in Language Models](https://arxiv.org/abs/2310.13548), ICLR 2024 |
| E22 | Shukla et al., [Security Degradation in Iterative AI Code Generation](https://arxiv.org/abs/2506.11022), 2025 |
| E23 | Wang, Pradel, Liu, [Are "Solved Issues" in SWE-bench Really Solved Correctly?](https://arxiv.org/abs/2503.15223), ICSE 2026 |
| E24 | Alexopoulos, Mitropoulos et al., [Contextual Bias in LLM-Assisted Security Code Review](https://arxiv.org/abs/2603.18740), 2026 |
| E25 | OpenSSF, [SLSA v1.2 Source requirements](https://slsa.dev/spec/v1.2/source-requirements) |
| E27 | Rossi et al. (Facebook), [Continuous Deployment of Mobile Software at Facebook](https://www.eecg.utoronto.ca/~stumm/Papers/Rossi-FSE-16.pdf), FSE 2016 |
| E28 | Google SRE Workbook, [Example Error Budget Policy](https://sre.google/workbook/error-budget-policy/), 2018 |
| E29 | Zeng et al., [Deep Just-in-Time Defect Prediction: How Far Are We?](https://par.nsf.gov/biblio/10273272), ISSTA 2021 |
| E30 | Lewis et al. (Google), [Does Bug Prediction Support Human Developers?](https://research.google/pubs/pub41145/), ICSE 2013 |
| E31 | Tufano et al., [Deep Learning-based Code Reviews: A Paradigm Shift or a Double-Edged Sword?](https://arxiv.org/abs/2411.11401), 2024 |
| E32 | Parasuraman, Manzey, [Complacency and Bias in Human Use of Automation](https://journals.sagepub.com/doi/10.1177/0018720810376055), Human Factors 2010 |
| E34 | Wolfe, Horowitz, Kenner, [Rare targets are often missed in visual search](https://pmc.ncbi.nlm.nih.gov/articles/PMC2701252), Nature 2005 |
| E36 | Gay, Salahirad, [How Closely are Common Mutation Operators Coupled to Real Faults?](https://research.chalmers.se/en/publication/536348), ICST 2023 |
| E37 | METR, [RCT on experienced developer productivity with AI](https://arxiv.org/abs/2507.09089), 2025 |
| E47 | [MCR-Bench](https://arxiv.org/abs/2608.27442), 2026 |
