# The Orchestrator is a deterministic loop, and judgment comes in through Deciders

A Python engine drives a Run as a deterministic loop, and only that loop changes the Run's state.
In the glossary this loop is the Orchestrator.
Every point where an outcome has to be chosen is modeled as a Decision with a closed list of outcomes from config, and a swappable Decider provides the implementation: a function, a large or small model, a Consilium, a human.
A Decider has no side effects and returns only the outcome with its reasons, and the Orchestrator carries the outcome out.
This keeps the control flow reproducible and lets it survive a crash through reconciliation, and a model can be put into any decision point or taken out of it by editing config.

## Considered Options

- The Orchestrator as a long-lived Claude Code session that drives the Run and calls the engine through a CLI or MCP, as in `implement-spec` and Superpowers.
  Rejected because such a session runs into context compaction and the shared subscription limits, `claude -p` does not pause the Run when it hits a limit, its state cannot be recovered after a crash, and round counters and model choice rest on prompt discipline (see `docs/research/prior-art-orchestrators.md`).
- The Orchestrator as an LLM role that the engine calls at decision points, with a separate word Engine for the loop.
  Rejected because the word "orchestrator" promises control while the role would only answer questions; Decision and Decider describe the same thing more precisely and are not tied to an LLM.
- A Gate as a role with a swappable implementation, an LLM judge included.
  Rejected because the value of a Gate is its firm verdict; a check by a model is modeled as a Decision, and the config shows which verdict is probabilistic.

## Consequences

- A Decision appears both in a Pipeline (it branches a Ticket's path) and in a Run's config (decisions over several Tickets, such as pausing or stopping the Run); the Decider contract is the same in both cases.
- A Consilium becomes an implementation of a Decider, and escalation to a human becomes a human Decider, to whom the Orchestrator hands the Decision through its CLI.
- If a model Decider returns an invalid outcome or crashes, the Orchestrator repeats the call, and after a limit of failures it moves the Decision to `escalate`.
- In v1 Deciders are mostly deterministic; a model sits in accepting a Ticket against the Spec, in triaging review findings and in the ruling after a round limit is hit.
- The v1 Spec describes the Orchestrator's CLI as the only interface for controlling a Run: status, cancelling a Ticket, overriding a Decision's outcome, changing limits.
  The Cockpit, that is a Claude Code session with a skill on top of this CLI, is built as the next step after v1 and does not change the core.
