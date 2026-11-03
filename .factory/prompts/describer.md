---
extends: describer
---
Each Ticket's `review` output lists its findings with an `outcome` and a `reason`.
Name in the PR the findings the reviewer skipped, each with its reason, and give the `door`, `blast_radius` and `danger_notes` of the reviewer as the ground of Merge Danger.

Write in the terms of `CONTEXT.md`.

When the diff touches more than five modules of `src/factory`, count each file as a module and the `cli` package as one, and say in `Summary` which step of `docs/architecture/coupling.md` explains the width, or what new seam the change found.

When the diff raises a ceiling of `tests/test_layers.py`, `MAX_CE`, an entry of `PACKAGE_CE`, `MAX_EDGES` or `MAX_MEAN_CE`, say in `Summary` which ceiling went up, from what to what, and why.
