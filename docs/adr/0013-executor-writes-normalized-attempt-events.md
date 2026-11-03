# The Executor writes the Attempt events, and no reader outside its adapter reads the agent's raw stream

Three readers read the raw `stream.ndjson` of an agent: `live.Silence` for stall detection, the Attempt renderer `render` for the owner's terminal, and through it the Workbench.
None of them could read a harness's events, so each asked the Executor: `Executor.is_progress` told progress by one line, `Executor.render_line` rendered one line, and the Workbench passed the Executor's name to the renderer's process, which made the Executor again from the registry of `wiring`.
That held only while one line of one file was all an Executor needed to tell what happened.
Codex splits it between its `--json` stdout and a rollout file (`docs/research/codex-cli-exec.md`, sections 2 and 5), so no reader outside its adapter can read "the stream" line by line.

So `ports/executor.py` defines the Attempt events, the factory's own format of what an agent did, one JSON object per line of `events.ndjson`:

- Every event has the format's version `v`, now 1, and its `type`, one of `progress`, `text`, `tool`, `usage`, `windows`, `result` and `error`.
- `progress` says the agent made progress, a new message or a tool result, which a heartbeat about a hanging command never is.
- `text` holds what the agent wrote, of `kind` `message`, `thinking` or `note`; `tool` holds a call by `name`, `target` and `status`, `started`, `done` or `failed`, with its `output` once it ends.
- `usage` holds what the Attempt spent, `windows` the subscription windows the harness reported, and `result` the agent's last word, after which no event follows.
- `error` holds a line the adapter cannot read, such as the crash message of an agent.

The Executor's handle writes the events as it reads its raw sources, and the raw stream stays next to them for debugging.
`Silence` counts `progress` events, `render` renders the events, and `Workbench.open_attempt` and the renderer's command line take the events file and no Executor.
An Attempt from before this decision has only its raw stream, which only Claude Code wrote then, so the renderer has `wiring` normalize it through the Claude Code adapter, once, into `events.ndjson`, at the first read.

The reasons hold without any agent harness:

- A harness that spreads what the factory needs over several sources, or writes a format that changes between its versions, is read in one place, the adapter that already starts and polls it.
- The renderer runs as a process of its own per Attempt, and once it reads a format the factory owns, it needs no Executor and no registry to read any Attempt.
- The events are a file, so a reader can follow them while the agent works, as `Silence` and the renderer do, and read them after the Run ends.

## Considered Options

- Keep `is_progress` and `render_line` on the port, and give them the paths of all of a harness's sources.
  Rejected because every reader would then follow several files of a harness it does not know, and the renderer would still need the Executor's name.
- Have each adapter write the rendered lines of the terminal instead of events.
  Rejected because stall detection and a future reader, such as a stats view or a second Workbench, need what happened and not how one terminal shows it.
- Normalize every old Attempt in a migration.
  Rejected because only the owner's terminal and `factory attach` read an old Attempt, and the renderer does it at the first read.

## Consequences

- An Executor writes `events.ndjson` from the start of an Attempt, empty at first, so a reader never takes a live Attempt for an old one.
- The events reach the file as often as the Orchestrator polls the handle, so the owner's terminal follows the agent at the pace of the poll, and a killed agent's last lines reach it when the handle kills it.
  An Orchestrator that dies leaves what its agent wrote since the last poll in the raw stream only.
- `AttemptStats` are still folded by the adapter out of its raw stream into the `AttemptFinished` of the Journal, which `factory stats` reads, so the events carry `usage` but no stats.
- A new event type or field raises `v` only when an old reader would misread it, because a reader skips a type it does not know.
