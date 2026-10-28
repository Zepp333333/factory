# Coding standards

Review applies these rules to a diff.
Each is a judgement call that `ruff`, `mypy` and `pytest` cannot make.
The words come from `CONTEXT.md`, and the decisions come from `docs/adr/`.

## Behaviour

- **ADR first.**
  The diff does what the ADRs it touches say, not only what the Ticket says.
  A Ticket is a slice of the Spec and may be looser than its ADR, for example a "threshold" where ADR 0005 names `min_changed` and `min_total`.
  Where the diff departs from an ADR, the PR names the ADR and says why.
- **Only the agent's own work spends attempts and Rounds.**
  A lost Sandbox, a missing Probe result or any other infrastructure loss is retried by `sandbox_retries`, or it stops the Run.
  It never fails a Gate or an Attempt.
- **Artifacts and Journal events describe what ran.**
  A Gate that judged another Gate's Probe writes under `from-<source>-probe-<n>/`, not under a `probe-<n>/` of its own.
  An event names the inputs it was judged on, such as `from` and `probe`.

## Structure

- **Gate kinds carry their differences.**
  Behaviour that differs by Gate kind lives as a field of `gates.Kind`, such as `check`, `reads_from` or `evaluates_changed_lines`.
  `config.py`, `pipeline_checks.py` and `orchestrator.py` read that field and hold no branch on a Gate kind's name.
- **Closed sets of names parse through `values.choice`.**

## Tests

- **Through the CLI.**
  A behaviour test drives `factory` with the harness in `tests/harness.py` and asserts on the Journal, the Artifacts and the git state.
  A unit test is for a pure evaluator, such as a Gate kind judging a fixture,
  or the charge of the Attempt module, which a test drives with a fake Executor and Sandbox, one test per cause class.
- **Every review finding gets a test that was red before the fix.**
- **Concurrency tests synchronize on state.**
  A test orders Tickets or Runs by polling for a Journal state or a file it creates, and never by a fixed delay.
  Every file that threads share, such as `factory.yaml`, is written before the threads start.
- **Fixtures are committed.**
  A fixture path that `.gitignore` covers gets its own `!` rule next to the one it overrides.
