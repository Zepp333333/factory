# Credits

`SKILL.md` is the skill `code-review` from [Matt Pocock](https://github.com/mattpocock)'s [skills](https://github.com/mattpocock/skills), version 1.3.1, under the MIT license in `LICENSE`.
It is copied word for word, except that it is renamed `review`, its description names the Ticket, and it differs from the original in four places.

- **Fixed point.**
  The original takes the fixed point from the user and asks for one when it is missing.
  Here the fixed point is the `base` of the Ticket in the Brief.
- **Spec.**
  The original looks for the spec in the Tracker, in a path the user passes, or under `docs/`, and asks the user when it finds none.
  Here the spec is `ticket.md` and `spec.md` from the Brief, and the skill never touches the Tracker.
- **Fix.**
  The original stops after the report.
  Here step 6 fixes the findings worth fixing and names each finding `fixed` or `skipped` with a reason.
- **Merge Danger.**
  Here step 7 takes the rubric from the skill `pr` and gives `door`, `blast_radius` and `danger_notes`.
