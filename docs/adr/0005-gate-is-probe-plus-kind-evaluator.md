# Gate - a probe in the Sandbox plus an evaluator from the factory's closed set of kinds

A Gate is given by a pair of `kind` and `with`, and a kind is a Python class in the factory's engine.
The kind knows the report format and decides pass or fail, while the command that produces the report comes from config as a shell string.
A Gate with `run` first launches a probe, that is a command in the Ticket's Sandbox that writes reports into the empty directory `$GATE_OUT`.
Then the kind's evaluator on the host reads these files and writes the verdict artifact, so the agent cannot fake the verdict and the engine does not depend on the toolchain.
A Gate is a pure function of `with` after `${vars.*}` substitution, the Workspace, the Ticket's base ref and the probe's output; it does not read the Run's history.
Position in the Pipeline is expressed only through `with`, because different positions are different nodes with different `with`.
One check per node; a chain of checks is a chain of nodes.

Kinds in v1:

- `command`: verdict by exit code, summary from the lines matching `match` and the tail of the log.
- `tests`: JUnit XML (a file or a glob), summary from the failed tests with their message and location.
- `coverage`: an `lcov`, `cobertura` or `jacoco` report, thresholds `min_changed` (lines the Ticket changed) and `min_total`, at least one of them required.
- `diff-limits`: number of files and lines, forbidden paths; computed on the host from git, without a probe.
- `document`: a file by glob, `new` (the Ticket must add it), required headings; checking what the document means is a Decision.

A prototype with kinds, examples for C++, Python, Kotlin and a document, verdict scenarios and broken configs lived on the branch `prototype/gate-params` in the original repository and was not carried over.

## Considered Options

- Shell only, verdict by exit code.
  Rejected because every project would parse the summary for the Rework Stage on its own, and its format would drift apart.
- Only Python plugins, one per tool.
  Rejected because a new language would need code in the engine, even though only the command differs.
- Kinds that the Target project puts in its own repository.
  Rejected because code written by agents would run on the host outside the Sandbox.
- Report paths relative to the Workspace.
  Rejected because an agent can commit a green `junit.xml`, and if the probe does not overwrite it, the Gate reads the fake.
- A third outcome `error` for infrastructure failures.
  Rejected because ADR 0004 keeps a Gate's outcomes closed to `pass` and `fail`; a failure is handled as a lost Attempt.
- One Gate with a list of checks.
  Rejected because the graph stops being explicit, and the checks share one verdict and summary.

## Consequences

- Every kind declares a schema for `with`, and the engine checks it before the Run starts, after `${vars.*}` substitution: an unknown field, a typo in `format`, a missing threshold, `run` on a host kind, `run` without `$GATE_OUT`.
- `coverage` can declare `from: <gate>` instead of `run` and parse another Gate's probe output without running the tests a second time.
  Before the start the engine checks that every path from any Stage to such a Gate passes through the source, otherwise the Gate would read a stale probe.
- A probe failure (the Sandbox died, the image did not come up) gives no verdict and spends no Round, and the Orchestrator repeats the Gate up to `attempts` times, then sends the Ticket to `escalate_to`.
  `attempts` is a field of the Gate node outside `with`, taken from `defaults` by default.
- A command timeout, a non-zero exit code with a green report and a missing report are `fail`, because the code caused them.
- The verdict artifact is shared by all kinds: `gate`, `kind`, `outcome`, `reasons`, `summary` (specific to each kind, of bounded length), `log`, `exit_code`, `duration`.
- The project file's `overrides` merge into the node recursively over dictionaries, while lists are replaced whole; this way a project changes one threshold without touching the rest of `with`.
- The probe runs in the same Ticket Sandbox where the agent worked, on the committed state (ADR 0009).
- Kinds for lint (SARIF) and typecheck are added when a concrete Pipeline needs them.
