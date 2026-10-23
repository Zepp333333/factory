# A human is a Decider, and the answer goes through the Orchestrator's Command inbox

A Run calls a human in only one way: a Ticket enters a Decision node with `decider: {kind: human}`.
All conditions for calling a human are set by Pipeline routes, not by separate engine mechanisms.
The round limit gives `exhausted`, the failure of all Attempts of a Round gives the new Stage outcome `failed`, and a Gate rejection goes into the fix loop and reaches a human only through `exhausted`.
The reviewer assesses the risk of a change through fields of its output schema, and a function Decider decides from these fields and the project's thresholds whether to route the Ticket to a human.
The human answers through a Command that any process puts into the Command inbox in the same SQLite, and the Orchestrator of its Run accepts or rejects it on a tick.
The Run's terminal shows the question card and takes the choice as input, but under the hood it puts into the inbox the same Command as `factory decide`.

## Considered Options

- A separate mechanism that stops a Ticket on engine conditions (risk class, Gate rejection, missing access).
  Rejected because these conditions are already expressed by Pipeline routes, and a second mechanism would give a second path to a human that is not visible in config.
- Treat running out of Attempts as the same `exhausted` as the round limit.
  Rejected because `exhausted` means "the agent worked and did not converge", while `failed` means "the agent could not work", and the human needs different things in these cases.
- Read the human's answer from a comment in the Tracker.
  Rejected because by ADR 0002 edits in the tracker do not steer a Run, and reading the tracker would bring the network and an author check into the loop.
  This can be added later as an adapter that puts the same Command into the inbox.
- Answer only with a command from another terminal, while the Run's terminal only signals.
  Rejected because the owner answers where they see "Needs You" (ADR 0006).
- The `decide` command writes the event to the Journal itself.
  Rejected because only the Orchestrator of its Run writes the Journal (ADR 0002), and the check "the Decision is still waiting and the outcome is in the list" must run in its transaction.
- Pass Commands through a unix socket of the Run's process.
  Rejected because a Command sent to a crashed process would be lost, while from the inbox it is applied after `resume`.
- Judge risk in a separate Stage with Role `risk-assessor`.
  Rejected because the reviewer has already read the diff; they can be split later by a config edit.

## Consequences

- A Stage has the outcomes `done` and `failed`, and a Stage with `rounds` also has `exhausted`; an unmatched `failed` goes to `escalate_to`.
- An Attempt failure gets a cause class (`auth`, `sandbox_start`, `executor_crash`, `timeout`, `agent_config`).
  `agent_config` means that the agent's init event differed from the factory's kit.
  If Attempts of at least two different Tickets fail in a row with the same class, the Orchestrator pauses the whole Run through a Run-level Decision with the outcomes `retry | abort`; `retry` spends no Rounds.
  `rate_limit` is not one of these classes; it gives a pause until `resetsAt`.
  An Attempt that ends `rate_limited` spends neither the Stage's `attempts` nor a Round, and new Attempts of all the Run's Tickets wait for `resetsAt`, or for a fixed pause if there is no `resetsAt` or it has already passed.
  Probes and Operations go on during the pause.
- A human Decision declares `inputs`, like a model Decision, and goes through the same check before the start.
  The Orchestrator renders a question card from the Brief into Artifacts: the Ticket, `cause`, the latest verdicts, links to the inputs and the closed list of outcomes with descriptions from config.
- For outcomes that send the Ticket back to work, `reasons` are required; the next Stage gets them as the input `cause`.
- While a Decision waits for a human, the Ticket's Sandbox is stopped, while the Workspace stays, and the human can commit into it.
  `accept` in the template Pipeline leads back through the Gates, and the Orchestrator records in the Journal the commits that appeared during the wait.
- The wait holds no subscription slot and has no timeout; the other Tickets go on, the Run's process keeps its heartbeat, and `resume` restores the waiting Decision from the Journal.
- The first valid Command on a Decision is accepted, and the second is rejected with the message "already decided"; `decide` waits for the mark and prints the result.
- All CLI Commands from ADR 0001 go through the inbox: answering a Decision, cancelling a Ticket, overriding an outcome, changing limits.
- The Run's terminal enters input mode only if its stdin is a TTY; with several waiting Decisions it shows a queue ordered by waiting time, one card at a time, and closes a card decided elsewhere on the next tick.
- The Tracker projection publishes the card as a comment and sets the label `factory:needs-human`, and after the answer removes the label and publishes the outcome with its reason; without OSC 7501 the Run's process prints BEL.
- The reviewer works with the factory's skill `review`, made from Matt Pocock's `code-review`: it takes the fixed point and the Spec from the Brief, and carries over the "Merge Danger" rubric from the skill `pr`.
  The reviewer's schema carries `door: two_way | one_way`, `blast_radius: local | module | cross_module | external` and `danger_notes`.
- The project file declares the release channel (`release: redeploy | shipped_binary`), and the reviewer assesses reversibility with regard to how the change reaches users.
  With `shipped_binary` a change in behavior visible to users or consumers gets `one_way`, while a refactoring that does not change behavior stays `two_way`.
  The thresholds of the route to a human are set in the function Decider's `with`; by default they are `one_way` or `blast_radius >= cross_module`.
