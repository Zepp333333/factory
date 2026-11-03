# AGENTS.md

## Language

Write everything that lands in the repo or on GitHub in English only.
This covers code, comments, docs, commit messages, branch names, PR titles and descriptions, review comments, issues, milestones, and labels.
Talk with the user in whatever language they use, but write these artifacts in English even when the discussion happens in another language.

## Agent skills

### Issue tracker

Issues live in GitHub Issues for Zepp333333/factory, managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Pull requests

A PR that touches more than five modules of `src/factory` says in its description which step of `docs/architecture/coupling.md` explains the width, or what new seam it found.
Count each file as a module and the `cli` package as one, as `make measure-coupling` does.
`tests/test_layers.py` holds the fan-out ceilings: a module imports at most 20 modules, and a package at most the modules outside it that `PACKAGE_CE` gives.
