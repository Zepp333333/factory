# A Fresh agent gets a Brief of declared inputs, and sessions are not resumed

Every Attempt starts with empty context, and everything the agent knows about earlier Stages comes through the Brief.
The Brief is an immutable file with a list of pointers that the Orchestrator writes for every Attempt and records in the Journal.
The Brief holds only the inputs the Stage declared in the Pipeline, required or optional.
The inputs sit in the Run's directory outside the Target project and are mounted into the Sandbox read-only, and the agent writes its output to its own Attempt directory.
The main output of a Stage is JSON in its role's schema through `--json-schema`, and Markdown files are allowed for prose.

## Considered Options

- Resume the session of the same implementer in the first fix Rounds, as Superpowers does.
  Rejected because session ids would have to be written to the Journal, an Attempt would be tied to a machine and an executor, and resume works differently in Codex.
  Continuity is kept instead by the required field `handoff` in the schema of the implementer role.
  Resume can be added later as a Stage flag if convergence is worse without it.
- Paste the contents of artifacts straight into the prompt.
  Rejected because the prompt bloats, and a Fresh agent by definition receives only pointers.
- Give every Stage everything the Ticket has accumulated.
  Rejected because limiting the reviewer to the diff and the criteria is a deliberate configuration choice, as at Cognition and Anthropic.
- Keep artifacts in a git-ignored directory of the Workspace.
  Rejected because the agent could rewrite review findings or a Gate verdict.

## Consequences

- The artifact of an Attempt is immutable and addressed by the key (Ticket, Stage, Attempt).
  An input takes the last finished Attempt by default and gets all Rounds of a Stage only when it asks explicitly.
- Gate verdicts and Decision outcomes with their reasons are artifacts of the same kind.
  A re-review gets the rulings and does not raise findings that already have one.
- A required input with no source makes the Pipeline invalid before the Run starts, and an optional input with no artifact simply stays out of the Brief.
- The Spec and the Ticket are rendered into files from the snapshot in the Journal once per Run, and the agent has no access to the Tracker.
- A Gate's artifact holds a deterministic summary and a pointer to the full log.
- The output schema is set per role, so two review positions in a Pipeline share one findings schema with stable ids.
- Passing code between Stages is settled in ADR 0009.
