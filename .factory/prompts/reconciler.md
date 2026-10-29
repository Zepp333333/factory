---
extends: reconciler
---
## In this repository

You resolve a text conflict in the Software Factory repository.

- Never run a real `claude` and never set `FACTORY_CONTRACT=1`.

### Run tests

- While you work, run only the tests your change touches, for example `uv run --locked pytest tests/test_x.py -k name`.
- Run the full suite once at the end, with `uv run --locked pytest -n auto`.
- Never wait for a command with `sleep`.
  A test that hangs fails at its 30 s timeout with a stack, so read the stack and find out why it hangs.

### Before you finish

Check the merged tree once at the end, when no markers are left, with these commands:

- `uv run --locked ruff check`
- `uv run --locked ruff format --check`
- `uv run --locked mypy`
- `uv run --locked pytest -n auto`

The Sandbox has no `python` in `PATH`, so run Python only through `uv run --locked`.
If a check command does not start, for example because it exits with 127, that is a failed check: say so in the `handoff` as a failure, not as a caveat, and do not hand off as if the tree were checked.
