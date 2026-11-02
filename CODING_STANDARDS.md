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
- **Each test names its behaviour in `tests/inventory.csv`.**
  A new test adds a row with the behaviour it guards in the words of `CONTEXT.md`, the source of that behaviour, and its level: `unit`, `harness` or `contract`.
  The source is an ADR, this file, a Spec, a Ticket or the review finding that asked for the test.
  `tests/test_suite_guard.py` fails on a collected test without a row and on a row whose test is gone.
  `make inventory` fills the measured columns, and docs/test-suite/baseline.md explains each column.
- **One behaviour, one test per level.**
  No two tests share a behaviour and a level, and `tests/test_suite_guard.py` checks the inventory for it.
  Cases that differ only in their input are one parametrized test.
  Scenarios that differ only by Tracker are one test parametrized by `github` and `gitlab`.
  A harness test asserts every outcome of its scenario, so a new outcome of a scenario that a test already runs joins that test instead of starting a Run of its own.
- **The lowest level that shows the behaviour.**
  A rule that a pure function decides gets a unit test of that function, not a Run through the harness.
- **Tests see what a user sees.**
  A test asserts on the CLI, the Journal, the Artifacts and the git state, so a refactor that keeps them passes.
  It does not pin private functions, wording that no user reads, or an order of events that no user relies on.
  No test imports or reaches a private name of `src/factory`, a name that starts with `_`, and `tests/test_suite_guard.py` fails on one.
  A unit test reaches its evaluator through public functions, and a rule without one gets a public function.
- **A deleted test leaves no branch unguarded.**
  A test that is the only one to take a branch goes only with a replacement that takes the branch and checks the same outcome.
  `make measure-coverage` maps each branch to the tests that take it.
- **The suite stays inside its time budget.**
  CI fails when the tests outside `tests/contract/` take longer together than `time_budget` in `pyproject.toml`.
  A slow test is made cheaper, in the test or in the harness, before the budget grows, and a PR that raises the budget says why.
- **A flaky test is a defect.**
  It is fixed in the PR that finds it, and never skipped or rerun.
- **Concurrency tests synchronize on state.**
  A test orders Tickets or Runs by polling for a Journal state or a file it creates, and never by a fixed delay.
  Every file that threads share, such as `factory.yaml`, is written before the threads start.
- **A result does not depend on the machine's speed.**
  `FakeClock` moves on every idle tick while a scripted agent runs, so a slower machine makes more ticks and a longer Run.
  A test does not pin a duration, a count of ticks or polls, or a time that the injected clock reaches through the Run's own sleeps.
  It derives the expected value from the Journal, for example through `Harness.run_took` and `Harness.ticket_took`,
  or bounds it only from the side the test controls, such as `duration >= 90` after a clock jump of 90 s.
  A time the test sets itself, such as a jump or a frozen clock, may be pinned.
  PR #137 failed CI because tests pinned `done in 0s` and `90 <= duration < 91`, and a slow runner made the Run last `1s`.
- **Fixtures are committed.**
  A fixture path that `.gitignore` covers gets its own `!` rule next to the one it overrides.
