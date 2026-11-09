# Exit check

Ticket #186 of Spec #171 guards the revamped suite and checks the Spec's exit criteria against [the baseline](baseline.md).
It measured on a 14-core Apple Silicon Mac, with the same commands as the baseline.

## Guards

| Rule | Where it is written | What fails |
|---|---|---|
| Each test names its behaviour and source in the inventory | `CODING_STANDARDS.md` | `tests/test_suite_guard.py` on a collected test without a row, a row without a behaviour or source, a level other than `unit`, `harness` or `contract`, or a row whose test is gone |
| One behaviour, one test per level | `CODING_STANDARDS.md` | `tests/test_suite_guard.py` on two test functions with the same behaviour and level |
| No private names of `src/factory` in tests | `CODING_STANDARDS.md` | `tests/test_suite_guard.py` on an import, an attribute of an imported name, or a `getattr` or `monkeypatch.setattr` target that names one |
| The suite stays inside its time budget | `time_budget` in `pyproject.toml`, 1200 s | CI, through `--time-budget` and `tests/time_budget.py` |

The guards run in `pytest`, so Gate `pytest` of Pipeline `self` applies them to every Ticket, and CI applies them to every push.
CI still lists the 20 slowest tests, and every run prints the summed test time against the budget.
The budget started at three quarters of the summed time of the baseline, the exit criterion on time.
It is now 1200 s, because the summed time of one commit varies by a third between CI runners and the suite grew.
Contract tests stay out of the sum, because they run only with `FACTORY_CONTRACT=1`.

No unit test needs a private name, so `CODING_STANDARDS.md` allows none.
A unit test reaches its evaluator through public functions.

## Tests no area Ticket judged

Tickets #184 and #185 judged the tests of their files.
Tests merged after the baseline, and the contract tests, had no row, so this Ticket judged them.

| File | Tests before | Tests after | Change |
|---|---|---|---|
| `test_file_issues.py` | 30 | 26 | The 4 refusals of a broken placeholder and the abandon without a `refused` route are one Run of a Spec with one Ticket per refusal |
| `test_phase_note.py` | 8 | 6 | The ticking CI line, the Orca record and the finished Run without a phase line are one Run |
| `test_process_leaks.py` | 3 | 3 | kept |
| `tests/contract/` | 31 | 31 | kept, one row each |
| `test_suite_guard.py` | | 5 | new |

## Exit criteria

| Criterion | Result |
|---|---|
| Every test has an inventory row with a behaviour and a source | 675 collected tests, 675 rows, checked by `tests/test_suite_guard.py` |
| No two tests share a behaviour and a level | checked by `tests/test_suite_guard.py` over every row, across both area Tickets |
| No test imports a private name of `src/factory` | checked by `tests/test_suite_guard.py`, none found |
| Summed test time is at most half the baseline | Met against 681.6 s, the baseline of the same command, with 290.0 to 309.1 s, or 43 to 45%. Not met against the 549 s the Spec quotes, see [Time](#time) |
| Branch coverage drops only by code the Spec deleted as dead | No branch lost its tests, see [Coverage](#coverage) |
| Mutation score is equal or higher | Not measured. The branch map stands in for it, as the Spec decided, see [Coverage](#coverage) |
| `pytest` in a Sandbox of 3 CPUs takes under 5 min | 35 to 36 s, see [Sandbox](#sandbox) |
| The suite passes 20 times in a row | 20 of 20 locally with `make test ARGS=--time-budget` and no reruns. CI ran once on PR #221 and passed, by the owner's choice |

## Time

`make measure-durations` ran three times on the same machine as the baseline.

| Number | Baseline | Exit |
|---|---|---|
| Collected tests | 911 | 675 |
| Summed test time | 681.6 s | 290.0 to 309.1 s |
| Wall time of the pytest session | 51.6 s | 23.8 to 24.6 s |

The Spec quotes a baseline of 549 s, which another command measured.
The exit check reaches it too only in the Sandbox below, where fewer workers share the CPUs.
The baseline of Ticket #182 explains why the exit check compares runs of `make measure-durations`.

The 20 runs of `make test` summed 283.2 to 323.6 s, because a busy machine slows each test.
CI enforces the budget on 4 workers, where PR #221 summed 210.5 s in 54.7 s of wall time, so CI keeps a margin of about a third.
Locally the budget is reported but not enforced.

## Sandbox

`pytest -n auto -q` ran three times in an `sbx` Sandbox of 3 CPUs and 3 GB, as Gate `pytest` of Pipeline `self` runs it, on a git checkout of this branch.

| Run | Wall time of `pytest` | Summed test time | Result |
|---|---|---|---|
| 1 | 36 s | 102.9 s | 644 passed, 31 skipped |
| 2 | 35 s | 102.7 s | 644 passed, 31 skipped |
| 3 | 35 s | 101.6 s | 644 passed, 31 skipped |

So `.factory/pipelines/self.yaml` sizes its timings to a check of about a minute.

| Setting | Before | After |
|---|---|---|
| `defaults.stall` | 30m | 10m |
| Gate `pytest`, `timeout` | 20m | 10m |

## Coverage

`make measure-coverage` ran with the pinned tools of the baseline.

| Number | Baseline | Exit |
|---|---|---|
| Branches covered | 1975 of 2238 | 1993 of 2246 |
| Branches taken by some test | 1974 | 1993 |

The baseline covered one branch, `src/factory/render.py:141->exit`, only outside any test, so the two rows differ by one.

No ticket of the Spec changed `src/factory`, so the Spec deleted no dead code.
Tickets #192, #198 and #200 changed seven files of `src/factory` after the baseline, which moved their branches.

- In every other file, each branch the baseline's tests took is still taken.
  Five branches gained a test, all in Ticket #185.
- The seven changed files leave untaken the same number of branches as before, or fewer.

The baseline replaced mutation testing with the branch map, because a full mutation run costs about 1,000 CPU hours.
The exit check reruns the same command, so the map is the mutation measure here too.
A test that left the suite was merged or deleted only after its assertions moved to the test that replaces it.

The coverage tools stay out of the dev dependencies.
No guard uses them, and `make measure-coverage` runs them at pinned versions through `uv run --with`.
