# Credits

`SKILL.md` is the skill `code-review` from [Matt Pocock](https://github.com/mattpocock)'s [skills](https://github.com/mattpocock/skills), version 1.3.1, under the MIT license in `LICENSE`, renamed `review` and changed in four places.

- **Fixed point.**
  The original compares the change against a ref the user names.
  Here the fixed point is the `base` of the Ticket in the Brief, and the change is `git diff <base>`.
- **Spec.**
  The original reads the spec from the Tracker.
  Here it is `ticket.md` and `spec.md` from the Brief, and the skill never touches the Tracker.
- **Fixing.**
  The original stops after the report.
  Here the agent goes on to fix the findings that are worth fixing, and names each finding `fixed` or `skipped` with a reason.
- **Merge Danger.**
  The agent takes the rubric from the skill `pr` and gives `door`, `blast_radius` and `danger_notes`.

`SKILL.md` was written in this repository from the shape of the original, two axes (standards and spec) reviewed by independent passes of the diff, without a copy of the original at hand.
It is not a word-for-word copy, and the text of the original may differ wherever the steps below read differently.
