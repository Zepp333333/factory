---
name: review-single
description: "Review the changes of a Ticket since its `base` along one axis: does the code match what the Ticket and Spec in the Brief asked for, and follow the repo's documented standards where they bear on it? Then fixes the findings worth fixing and judges Merge Danger. Use when reviewing the change of a Ticket with a single review."
---

One-axis review of the diff between `HEAD` and the `base` of the Ticket in the Brief.

The axis is one question: is this change what the Ticket and Spec asked for, written the way this repo's documented standards and the code around it expect?
Do not split it into sub-agents, and do not use the Tracker or any `gh` command. The Brief holds everything the review needs.

## Process

### 1. Pin the fixed point

The fixed point is the `base` commit of the Ticket in the Brief.

Capture the diff command once: `git diff <fixed-point>...HEAD` (three-dot, so the comparison is against the merge-base). Also note the list of commits via `git log <fixed-point>..HEAD --oneline`.

Confirm the fixed point resolves (`git rev-parse <fixed-point>`) and the diff is non-empty before you go on.

### 2. Gather the sources

The `ticket.md` and `spec.md` files of the Brief are the spec: the Ticket's acceptance criteria are the checklist.
Read anything in the repo that documents how code should be written, such as `CODING_STANDARDS.md` or `CONTRIBUTING.md`.

### 3. Review

Read the diff against the sources and list every finding: a requirement that is missing or partial, behaviour nobody asked for, an implementation that looks wrong, or a breach of a documented standard.
Skip anything tooling already enforces.
Give every finding an `id`, the `axis` you reviewed along, its `file` and `line`, and a one-line `summary`.

### 4. Fix

Fix every finding that is worth fixing. A finding is worth fixing when the fix is small, local, and does not change what the Ticket asks. Skip a finding when fixing it needs a decision that belongs to the owner, widens the Ticket, or touches code outside the change.

Give every finding one of two outcomes:

- `fixed`: you changed the code, and the `reason` says what you did.
- `skipped`: you left it, and the `reason` says why.

Run the project's checks after your edits and leave them green.

### 5. Merge Danger

Take the Merge Danger rubric from the skill `pr` and judge the change as it stands after your fixes. Give:

- `door`: `two_way` or `one_way`.
- `blast_radius`: `local`, `module`, `cross_module` or `external`, widest last.
- `danger_notes`: the potential ramifications of the merge, or an empty string.
