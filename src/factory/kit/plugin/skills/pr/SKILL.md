---
name: pr
description: Write the description of a pull request so that its owner can decide whether to merge it. Use when asked to describe a PR or to judge how dangerous a merge is.
---

# PR description

Write the description for the reader who decides whether to merge, and who has not seen the work.
Read the change itself with `git diff <base>...<head>`, and the Brief's Gate verdicts and Stage outputs for what was proved.
Use the words of `CONTEXT.md`, and cite nothing you did not read.

A description has three sections, in this order, each a `##` heading.

## Summary

The smallest picture of the change that makes it clear.
Pick the one form that fits and use only that one:

- a diff sketch: a few lines of the diff that carry the idea, with the noise cut out,
- a call tree: who now calls whom, for a change in behaviour,
- a file tree: what was added, moved or removed, for a change in structure,
- a Mermaid diagram: for a change in flow or in states.

Add one or two sentences that say what the change is for. Do not retell the diff.

## Evidence

What shows that it works, as before and after.
Take it from the Gate verdicts and test results of the Run, and quote the numbers or names that changed:
the test that failed and now passes, the coverage before and after, the build that was red.
Say plainly what has no evidence.

## Merge Danger

What merging costs if it is wrong. Give both entries:

- **Door**: `one-way` when the change cannot be taken back once it reaches users, such as a data migration, a published API, a shipped binary or a deleted file nobody has a copy of. `two-way` when reverting the merge undoes it. Give the reason in one sentence.
- **Blast Radius**: what breaks if the change is wrong, as one of `single-file`, `module`, `cross-module` or `external`, with the names of what would break.

Rate the danger from the change, not from how confident the author sounded.
