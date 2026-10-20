# Orca is one implementation of the Workbench, and a Run is visible through text and OSC 7501

The environment where the owner sees a Run's Workspaces and Attempts is called the Workbench.
The core reaches it only through the `Workbench` interface, and Orca is one of its implementations on a par with a plain terminal.
The Workbench does four things: it creates a Workspace, launches an Attempt so that it can be watched, marks the Ticket's state on its card and answers reconciliation about whether all of this is in place.
`OrcaWorkbench` does this through `orca worktree create`, `orca terminal create`, `orca worktree set` and `orca terminal list`.
`TerminalWorkbench` uses `git worktree add`, a background process with a log and the `attach` command, and it has no cards.
The Workbench is independent of the Sandbox, so any pair works, such as Orca with Docker or a plain terminal with Docker.

The Run itself is visible without a Workbench.
The Run's process always prints a text tree: the Run, its Tickets, the current Stage and Round, and a Decision waiting for a human.
If the terminal answers the probe `OSC 7501;?`, the same process writes the tree as Program Status Protocol records: the root `app=factory` is the Run, and the children with `id=<ticket>` are the Tickets.
When a Decision waits for a human Decider, both the Ticket and the root get `state=blocked:kind=question`, because Orca shows "Needs You" only from the root's state.
The core has no check for "are we running in Orca"; the terminal's answer to the probe makes the choice.

An Attempt is launched as `claude -p --output-format stream-json`, and a renderer runs in its terminal.
The renderer prints the assistant's text, tool calls, short results, `rate_limit_event` and the final result, while the raw NDJSON goes to Artifacts unchanged.

The prototype lived on the branch `prototype/run-view` in the original repository and was not carried over.
It was tested on Orca 1.4.232: the `factory` row with subagents, "Waiting for input" in the sidebar and the counter in the Agent Dashboard are visible both from the host and through `docker exec -it`.

## Considered Options

- Call Orca directly from the core, as the Orca ticket proposed.
  Rejected because the factory would then work only while Orca is running, and the owner requires a plain terminal as an equal environment.
- Take Needs You from an interactive Claude Code in the Sandbox.
  Rejected because agents work through `claude -p`, which has no permission prompt: a refusal arrives as a `tool_result` with the error "This command requires approval", and it is the Orchestrator that calls the human, through a Decision (ADR 0001).
- Orca's managed hooks, which put the prompt, the tool and the last answer on the card.
  Deferred to a separate issue [Rich Attempt card in Orca through managed hooks](https://github.com/Zepp333333/factory/issues/20), because Orca's hooks live in `~/.claude`, which the factory's agent does not see, and from the Sandbox it would need a path to a loopback server on the host.
- Mark "a human is needed" only through `orca worktree set --unread`.
  Rejected as the main channel, because it is only a dot on the card without Needs You, notifications and an agent row, and it exists only in Orca.
- Always write OSC 7501, without a probe.
  Rejected because a terminal without support for the protocol may show garbage, and the probe costs one line.

## Consequences

- OSC 7501 needs Orca 1.4.232 or newer, which shipped PRs [#30261](https://github.com/stablyai/orca/pull/30261) and [#30262](https://github.com/stablyai/orca/pull/30262); an older Orca does not answer the probe, and the text tree remains.
- Orca shows only live children: a Ticket in `done` or `error` leaves the list of subagents, while the Run's final result stays on the root, and `error` looks like a red dot on the tab.
- All children hang on the terminal of the Run's process, so in Orca the tree is visible in the Workspace where that terminal is open, not on the cards of the Tickets' Workspaces; the Tickets' cards get their Stage through `orca worktree set`.
- OSC 7501 and the probe pass through `docker exec -it` in both directions, so the Run's process and the Attempt's renderer can also work inside a Sandbox.
- The command that launches an Attempt through Docker sets `DOCKER_CLI_HINTS=false`, otherwise an ad for Docker Debug lands in the terminal after `docker exec` exits.
- In `stream-json` mode thinking blocks arrive with empty text, and the renderer skips them.
- How a human answers a Decision is settled by the ticket [When and how a Run calls a human](https://github.com/Zepp333333/factory/issues/11).
