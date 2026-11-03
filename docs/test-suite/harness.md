# Cheaper harness

Ticket #183 of Spec #171 cut the costs that the [baseline profile](baseline.md#harness-profile) names for every harness test.
It changes only the shared harness, `tests/harness.py` and `tests/scripted_agent.py`, and no test changes what it asserts.

## Changes

| Change | Cost in the profile | Before | After |
|---|---|---|---|
| `Harness.end` lists processes once when the test left none | `ps` from the harness | 50.1 s, 2.1 calls per test | 23.6 s, 1.1 calls per test |
| The Target project gets its URL rewrites and loses its remotes by writes to `.git/config` | git | 138.2 s, 38.0 calls per test | 116.6 s, 33.5 calls per test |
| A tick takes no real time while only parked agents run, and an agent waiting for its `wait_for` file counts as parked | idle ticks | 18.2 s | 11.3 s |
| With a `FakeTty` the probe of the Run view waits 0.05 s instead of 0.5 s | Python of the factory and the harness | 40.2 s | 31.6 s |

`make measure-profile` measured both columns on `c108bf0`, one after the other on the same machine, over the same 624 harness tests.
The harness tests took 419.4 s before and 349.1 s after, 17% less.

## Suite

`make measure-durations` ran three times for each side, alternating, on a 14-core Apple Silicon Mac.

| Number | Before | After |
|---|---|---|
| Summed test time | 619.5 to 648.6 s | 582.7 to 589.5 s |
| Wall time of the pytest session | 46.5 to 47.9 s | 43.7 to 44.2 s |

`make test` ran back to back on a loaded machine in 58.6 and 64.5 s before, and in 59.4 and 61.0 s after.
The parallel suite is bound by CPU, and real sleeps cost no CPU, so its wall time gains less than the serial profile.
The suite passed 5 times in a row with `make test` and no reruns.

## Coverage

Line and branch coverage stay the same, 7467 of 7872 lines and 1989 of 2248 branches.
No branch lost the only test that takes it, and `src/factory/attempt.py:744->745` gained a second one.
The branch map stands in for the mutation score, as [the baseline](baseline.md#branch-map) explains.

## Left for later

The Attempt renderer, `python -m factory.render`, is the largest single program at 73 s.
Each Attempt waits about 120 ms for it at the end, because it imports `factory.wiring` and with it `jsonschema`.
Since ADR 0013 the renderer reads the Attempt events and needs `wiring` only to normalize an Attempt from before them, so a renderer that imports `wiring` only for such an Attempt is a Ticket of its own.
