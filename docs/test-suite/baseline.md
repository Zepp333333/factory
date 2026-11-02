# Test suite baseline

The baseline of Spec #171, measured by Ticket #182 on `5d11325` of `main`.
Every later Ticket of the Spec judges tests against these numbers, and the final Ticket reruns the same commands.
The final Ticket's results are in [exit.md](exit.md).

- Machine: Apple Silicon Mac, macOS, Python 3.14.2, pytest 9.1.1, pytest-xdist with `-n auto`.
- Raw results: [`baseline/`](baseline/), with `summary.json` holding every number below.
- Inventory: [`tests/inventory.csv`](../../tests/inventory.csv), one row per test.

## Commands

| Measurement | Command | Output in `build/measure/` |
|---|---|---|
| Durations | `make measure-durations` | `junit.xml` |
| Coverage and the branch map | `make measure-coverage` | `coverage.json`, `.coverage`, `branches.json` |
| Harness profile | `make measure-profile` | `profile.jsonl` |
| Baseline and inventory | `make baseline` | writes `docs/test-suite/baseline/` and `tests/inventory.csv` |
| Inventory only | `make inventory` | writes the measured columns of `tests/inventory.csv` and keeps the baseline |
| All of the above | `make measure` | |

The coverage tools run at pinned versions through `uv run --with`, so they stay out of the dev dependencies.
The scripts live in [`tools/measure/`](../../tools/measure/).

## Durations

| Number | Value |
|---|---|
| Collected tests | 911 |
| Passed | 880 |
| Skipped | 31, all contract tests without `FACTORY_CONTRACT=1` |
| Failed | 0 |
| Summed test time | 681.6 s |
| Wall time of the pytest session | 51.6 s |
| Tests over 1 s | 279, together 512.3 s |
| Tests over 2 s | 90 |

`make measure-durations` runs the suite as `make test` does, in parallel on every CPU.
Its wall time is the pytest session from the junit report, without the start of `uv` and Python.
Each test runs slower in parallel than alone, because the workers share the CPUs and the disk.
So the summed time of 681.6 s is higher than the 549 s the Spec quotes, and the final Ticket must compare it with a run of the same command.

The Spec was written against 807 tests.
Tickets merged after it added the rest, as `pytest --collect-only` counts at each merge on `main`:

| Change | Tests after it |
|---|---|
| Spec #171 written | 807 |
| #179, layer the module graph | 860 |
| #181, GitLab Tickets and Operation `file_issues` | 895 |
| #189, Describer Role checks the PR body | 907 |
| #190, GitLab manual jobs | 908 |
| #193, end orphaned renderers and agents | 911 |

The 20 slowest tests are in `summary.json` under `durations.slowest`.
The slowest is `tests/test_file_issues.py::test_github_tries_a_failed_call_again` at 4.7 s.

## Coverage

| Number | Value |
|---|---|
| Tool | coverage.py 7.17.0 with pytest-cov 7.1.0 |
| Lines | 7429 of 7839, 94.77% |
| Branches | 1975 of 2238, 88.25% |

Per-file numbers are in `baseline/coverage.csv`.

## Branch map

`make measure-coverage` records one coverage context per test, and `tools/measure/branches.py` turns it into a map from each branch to the tests that take it.
A branch is an arc out of a line with more than one way out, as coverage.py counts it, and the map uses coverage.py's own analysis of each test, so its counts match the report.

| Number | Value |
|---|---|
| Branches taken by some test | 1974 |
| Branches taken only outside any test | 1, `src/factory/render.py:141->exit` |
| Branches no test takes | 264 |
| Branches only one test takes | 251 |
| Tests that alone take some branch | 168 |
| Passed tests that take no branch | 144 |
| Passed tests that run no line of `src/factory` | 27, such as the source scans of `test_layers.py` and `test_context_words.py` |

`baseline/branches.csv` lists every branch with the number of tests that take it, and names those tests when there are at most 10.
The full map names about 265,000 pairs of branch and test, about 25 MB, so `make measure-coverage` rebuilds it in `build/measure/branches.json` instead.

The run sets `COVERAGE_CORE=ctrace`.
The default `sysmon` core of Python 3.14 records each line once, so it drops every test that takes a line after the first one did.

The map replaces mutation testing in this Spec.
mutmut 3.8.1 makes 21,316 mutants of `src/factory`, and running every test that reaches each of them would take about 1,000 CPU hours.
A branch that only one test takes shows which tests a deletion would leave unguarded.
Coverage shows that a test reaches code, but not that it checks the result, so a verdict also reads the test's assertions.

## Harness profile

`make measure-profile` runs the suite in one process, so the shares do not depend on how the workers share the CPUs.
It counts only the time of the 614 tests that build a `Harness`, 376 s in all.
Each wrapped call counts only its own time, without the wrapped calls inside it.

| Category | Seconds | Share | Calls per test |
|---|---|---|---|
| git | 122.0 | 32.5% | 37.6 |
| Other subprocesses | 116.0 | 30.8% | |
| Fake CLIs (`fake_gh`, `fake_glab`, `fake_orca`, `fake_sbx`) | 55.2 | 14.7% | 3.0 |
| Python of the factory and the harness | 37.2 | 9.9% | |
| Scripted agent | 21.7 | 5.8% | 1.2 |
| Idle ticks of `FakeClock.sleep` | 16.6 | 4.4% | 13.0 |
| SQLite | 7.6 | 2.0% | 227.7 |

Two programs make up the other subprocesses:

| Program | Seconds | Calls per test |
|---|---|---|
| `python -m factory.render`, the Attempt renderer | 67.5 | 1.2 |
| `ps` from `tests/harness.py`, the check for processes a test leaves behind | 48.0 | 2.1 |

Per-test numbers are in `baseline/profile.csv`.
Git time includes the git commands of the factory and of the harness setup, but not the git that runs inside a fake CLI.
The scripted agent counts the time a test waits in `ScriptedHandle.poll` for the agent to write its files.

## Inventory

`tests/inventory.csv` has one row per collected test, sorted by file and line.

| Column | Meaning | Filled by |
|---|---|---|
| `test` | pytest node id | this Ticket |
| `behaviour` | the behaviour the test guards, in the words of `CONTEXT.md` | area Tickets |
| `source` | ADR, `CODING_STANDARDS.md`, Spec or review finding behind the behaviour | area Tickets |
| `level` | `unit`, `harness` or `contract` | this Ticket |
| `only_branches` | the branches only this test takes, as `path:from->to` | this Ticket |
| `duration` | seconds in the durations run | this Ticket |
| `verdict` | keep, merge, move down, rewrite, delete or speed up | area Tickets |

A test is `contract` when it lives in `tests/contract/`, `harness` when it builds a `Harness` in the profile run, and `unit` otherwise.
The levels count 266 unit, 614 harness and 31 contract tests.
`make baseline` rewrites the measured columns and keeps `behaviour`, `source` and `verdict` of every test already in the file.
