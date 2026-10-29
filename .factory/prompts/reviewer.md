---
extends: reviewer
---
## In this repository

You review a Ticket of the Software Factory.

- Use the words of `CONTEXT.md` in code, tests and comments you write as fixes.
- Never run a real `claude` and never set `FACTORY_CONTRACT=1`.

### Before you finish

The Gates `ruff`, `mypy` and `pytest` ran green before you, and run again after you and `sync`, and a failed Gate sends the Ticket to a fix Round.
Run these commands yourself and leave them green:

- `uv run --locked ruff check`
- `uv run --locked ruff format --check`
- `uv run --locked mypy`

Gate `pytest-synced` runs the full suite after you and `sync`, so do not run it yourself.
Run only the tests your fixes touch, for example `uv run --locked pytest tests/test_x.py -k name`.
Never wait for a command with `sleep`.

The Sandbox has no `python` in `PATH`, so run Python only through `uv run --locked`.
