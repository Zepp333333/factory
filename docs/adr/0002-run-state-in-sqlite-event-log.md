# Run state lives in a SQLite event log, and the tracker gets only a projection

The source of truth about a Run is the Journal, an event log in a single SQLite database per machine, and only the Orchestrator of that Run writes to it.
The tables of current state are projections of the Journal, updated in the same transaction through the single write path `append(event)`.
A Run reads the Spec and the Ticket graph from the Tracker once at its start, saves a snapshot into the Journal and does not see later edits in the tracker.
What goes back to the Tracker is a projection: a delivery cursor walks the Journal, picks the events meant for people and retries delivery, so an unreachable tracker never stops a Run.
Each Run is driven by its own Orchestrator process with no daemon; the subscription limit shared by the account is kept in a coordination table of the same database, so taking a slot, the claim and recording the Attempt happen in one transaction.

## Considered Options

- The Tracker as the control plane, as Symphony does with Linear.
  Rejected because the tracker has many writers while ADR 0001 requires one; the GitHub and GitLab APIs have no transactions, while the claim, the Attempt and the Stage start must happen atomically; round counters, session ids and `resets_at` do not fit into labels and comments; the Tracker comes in three kinds (GitHub, GitLab, Local), and the store would have to be built three times; the network and rate limits would end up inside the loop.
  Symphony gets by with the tracker because its states are coarse and it has no review Rounds, Gates or rulings.
- State files in the repo.
  Rejected because every Stage transition would become a commit, and such commits are noisy and conflict with merging Tickets.
- A separate database for each Run.
  Rejected because the shared subscription limit needs a shared store anyway, and then the slot and the Attempt end up in two files with no common transaction.
  Isolation and a simple per-Run database archive do not outweigh this; a Run archive comes from an export.
- Mutable tables without an event log.
  Rejected because the Run history for convergence metrics would have to be kept separately, while with an event log changing a projection becomes a rebuild rather than a data migration.
- Engine input as a file exported from the tracker by a separate command.
  Rejected because the Local implementation of the Tracker gives the same: it serves both as the working mode for a Spec in scratch and as a test fake without mocks.

## Consequences

- Edits in the tracker do not control a Run: closing an issue does not cancel a Ticket, and cancelling goes only through the Orchestrator's CLI.
  A rule "closing an issue is a signal to cancel" can be added later.
- The full picture of a Run is visible through the CLI `status`; the tracker and the Orca cards show a summary.
- What must stay stable is the event schema; the engine does not migrate the database while it holds live Runs.
- A lease with a heartbeat determines whether a Run is alive; any Orchestrator sees a stale heartbeat, frees the slots of the abandoned Run and writes `RunOrphaned`.
  Only an explicit `resume <run_id>` command takes an abandoned Run up again.
- On every tick the Orchestrator reconciles Attempts with processes, Sandboxes, worktrees in Orca and git branches; a mismatch gives `AttemptLost` and an ordinary retry.
- An agent that outlived its Orchestrator is not adopted: it is stopped, the Workspace is rolled back to the commit at the start of the Stage, and the Stage goes on with a new Attempt.
- Logs, transcripts and Gate output sit in the Run's directory on disk outside the Target project; the Journal keeps pointers to them.
