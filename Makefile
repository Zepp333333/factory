# Every tool runs through uv from the locked environment, so local runs match CI.
# Pass extra arguments with ARGS, for example `make test ARGS="tests/test_cli.py -k status"`.

PYTHON ?= 3.14
RUN := uv run --locked
ARGS ?=

.DEFAULT_GOAL := help

.PHONY: help venv install lock upgrade upgrade-package add add-dev \
	lint fix types test test-serial test-contract factory-check check clean distclean

help: ## Show this list
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# Environment

venv: ## Recreate .venv and install all dependencies, PYTHON=3.14 by default
	rm -rf .venv
	uv venv --python $(PYTHON)
	uv sync --locked
	@echo "Activate it with: source .venv/bin/activate"

install: ## Install the locked dependencies into .venv
	uv sync --locked

# Dependencies

lock: ## Update uv.lock after editing pyproject.toml by hand
	uv lock
	uv sync --locked

upgrade: ## Upgrade every dependency to its latest allowed version
	uv lock --upgrade
	uv sync --locked

upgrade-package: ## Upgrade one dependency, PKG=name
	@test -n "$(PKG)" || { echo "Usage: make upgrade-package PKG=name"; exit 1; }
	uv lock --upgrade-package $(PKG)
	uv sync --locked

add: ## Add a runtime dependency, PKG=name
	@test -n "$(PKG)" || { echo "Usage: make add PKG=name"; exit 1; }
	uv add $(PKG)

add-dev: ## Add a dev dependency, PKG=name
	@test -n "$(PKG)" || { echo "Usage: make add-dev PKG=name"; exit 1; }
	uv add --dev $(PKG)

# Checks

lint: ## Check lint and formatting with ruff
	$(RUN) ruff check
	$(RUN) ruff format --check

fix: ## Fix lint findings and format the code with ruff
	$(RUN) ruff check --fix
	$(RUN) ruff format

types: ## Type-check with mypy
	$(RUN) mypy

test: ## Run the tests in parallel
	$(RUN) pytest -n auto $(ARGS)

test-serial: ## Run the tests in one process, for debugging
	$(RUN) pytest $(ARGS)

test-contract: ## Run the contract tests against real services, docs/self-run.md lists what they need
	FACTORY_CONTRACT=1 $(RUN) pytest $(or $(ARGS),tests/contract)

factory-check: ## Validate factory.yaml and its Pipeline
	$(RUN) factory check

check: lint types test factory-check ## Run every check that CI runs

# Cleanup

clean: ## Remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist
	find . -path ./.venv -prune -o -name __pycache__ -type d -exec rm -rf {} +

distclean: clean ## Remove caches and .venv
	rm -rf .venv
