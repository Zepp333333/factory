---
name: review
description: "Use when reviewing the change of a Ticket against its Spec, and fixing the findings worth fixing."
---

Review the change of the Ticket, then fix what is worth fixing, in one go.
The Brief gives the fixed point, the Spec and the Ticket.
Do not use the Tracker or any `gh` command.

## Inputs

- **Fixed point:** the `base` commit of the Ticket in the Brief.
  The change under review is everything between `base` and the working tree: `git diff <base>` and `git log <base>..HEAD`.
- **Spec:** the `spec` and `ticket` files of the Brief.
  The Ticket's acceptance criteria are the checklist.
- **Standards:** `AGENTS.md`, `CONTEXT.md`, the ADRs in `docs/adr/` that the change touches, and the style of the code around it.

## Review

Review on two axes, one pass of the diff per axis, and do not let one pass anchor the other.

1. **Standards.**
   Does the change follow the project's own rules and the code around it?
   Look for wrong domain words, a new idiom where the code has one, comments and docstrings out of step, dead code, a missing or brittle test, a test that drives a class instead of the public seam, and a change that no Gate would catch but a reader would.
2. **Spec.**
   Does the change do what the Ticket asks, all of it and no more?
   Check each acceptance criterion against the diff and name those that are not met, met only by a test that cannot fail, or exceeded.
   Look also for behavior the Spec forbids, an outcome with no route, and a path that Gates miss.

Report only a finding you can point at: a `file`, a `line`, and what is wrong in one sentence.
Read the code before you write a finding, and drop it when the code answers it.
Do not report what `ruff`, `mypy` and `pytest` already catch.

## Fix

After the report, fix every finding that is worth fixing.
A finding is worth fixing when the fix is small, local, and does not change what the Ticket asks.
Skip a finding when fixing it needs a decision that belongs to the owner, widens the Ticket, or touches code outside the change.

Give every finding an `id` and one of two outcomes.

- `fixed`: you changed the code, and the `reason` says what you did.
- `skipped`: you left it, and the `reason` says why.

Run the project's checks after your edits and leave them green.

## Merge Danger

Take the rubric from the skill `pr` and judge the change as it stands after your fixes.

- `door`: `one-way` or `two-way`.
  A change you can walk back is a two-way door.
  A destructive action or a hard-to-reverse decision is a one-way door.
- `blast_radius`: one word for the scope of the change, such as `local`, `module`, `cross-module` or `public`.
- `danger_notes`: the ramifications of the merge that the owner should know, or an empty string.
