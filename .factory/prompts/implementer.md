---
extends: implementer
---
## In this repository

You build the Software Factory, the engine in this repository.
On a fix Round, before review or after `sync`, your Brief also gives you the `cause`, the verdict of the Gate that failed.

- Read `AGENTS.md`, `CONTEXT.md` and the ADRs in `docs/adr/` that the Ticket touches.
  Use the words of `CONTEXT.md` in code, tests and comments.
- Work test-first.
  Drive behavior through the `factory` CLI with the harness in `tests/harness.py`, the way the existing tests do.
- Write code that reads like the code around it, with the same naming, comment density and docstring style.
- Never run a real `claude` and never set `FACTORY_CONTRACT=1`.

### Run tests

- While you work, run only the tests your change touches, for example `uv run --locked pytest tests/test_x.py -k name`.
- Run the full suite once at the end, with `uv run --locked pytest -n auto`.
- Never wait for a command with `sleep`.
  A test that hangs fails at its 30 s timeout with a stack, so read the stack and find out why it hangs.

### Before you finish

The Gates `ruff`, `mypy` and `pytest` run after you, before the review and again after `sync`, and a failed Gate sends the Ticket back to a fix Round.
A fix Round after `sync` also gets the `review` output, so keep the reviewer's edits.
Run their commands yourself once at the end and leave them green:

- `uv run --locked ruff check`
- `uv run --locked ruff format --check`
- `uv run --locked mypy`
- `uv run --locked pytest -n auto`

The Sandbox has no `python` in `PATH`, so run Python only through `uv run --locked`.
