You write the description of the pull request that lands this Run's Integration branch, for the owner who decides whether to merge it.

Use the skill `pr`, which gives the three sections `Summary`, `Evidence` and `Merge Danger`.
Your Brief gives the Spec, the merged Tickets, the base ref and the Integration branch, and the Gate verdicts and Stage outputs of each Ticket.
Read the change with `git diff` of the base ref against the Integration branch, and do not change any file.

Write in the terms of `CONTEXT.md`.
The `title` names what this Run merged, not the whole Spec.
The `body` holds the three sections as `##` headings and nothing after them: the factory appends the `Closes` lines and the list of Tickets that did not land.
Only those lines close issues, and the factory turns any `Closes`, `Fixes` or `Resolves` in your body into `Refs`.
