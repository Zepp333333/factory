# Every tool runs through uv from the locked environment, so local runs match CI.
# Pass extra arguments with ARGS, for example `make test ARGS="tests/test_cli.py -k status"`.

PYTHON ?= 3.14
RUN := uv run --locked
ARGS ?=

.DEFAULT_GOAL := help

.PHONY: help venv install lock upgrade upgrade-package add add-dev \
	lint fix types test test-serial test-contract factory-check check \
	measure measure-durations measure-coverage measure-profile baseline inventory clean distclean

help: ## Show this list
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

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

# Test suite baseline, docs/test-suite/baseline.md explains each measurement.
# The tools run at pinned versions next to the locked environment, so they stay out of the dev dependencies.

MEASURE := build/measure
COVERAGE_TOOLS := --with coverage==7.17.0 --with pytest-cov==7.1.0
COVERAGE_CONFIG := $(CURDIR)/tools/measure/coverage.toml

measure: measure-durations measure-coverage measure-profile baseline ## Run every measurement and write the baseline

measure-durations: ## Time every test as `make test` runs it
	mkdir -p $(MEASURE)
	$(RUN) pytest -n auto -o junit_family=xunit1 --junitxml=$(MEASURE)/junit.xml

# The default sysmon core of Python 3.14 records a line only once, so it loses the tests that take it after the first.
# A process a test spawns measures itself through COVERAGE_PROCESS_START, and the session combines its data file.
measure-coverage: ## Measure line and branch coverage of src/factory and map each branch to the tests that take it
	mkdir -p $(MEASURE)
	rm -f $(MEASURE)/.coverage $(MEASURE)/.coverage.*
	COVERAGE_CORE=ctrace COVERAGE_FILE=$(CURDIR)/$(MEASURE)/.coverage COVERAGE_PROCESS_START=$(COVERAGE_CONFIG) \
		$(RUN) $(COVERAGE_TOOLS) pytest -n auto \
		--cov --cov-config=$(COVERAGE_CONFIG) --cov-context=test --cov-report=json:$(MEASURE)/coverage.json --cov-report=term
	$(RUN) $(COVERAGE_TOOLS) python tools/measure/branches.py $(MEASURE)/.coverage $(MEASURE)/branches.json

measure-profile: ## Split the time of each test into git, SQLite, subprocesses, idle ticks and the scripted agent
	mkdir -p $(MEASURE)
	rm -f $(MEASURE)/profile.jsonl
	PYTHONPATH=$(CURDIR)/tools/measure HARNESS_PROFILE=$(CURDIR)/$(MEASURE)/profile.jsonl \
		$(RUN) pytest -p harness_profile

baseline: ## Write docs/test-suite/baseline/ and the measured columns of tests/inventory.csv from build/measure
	$(RUN) python tools/measure/report.py $(MEASURE)

inventory: ## Write only the measured columns of tests/inventory.csv from build/measure, and keep the baseline
	$(RUN) python tools/measure/report.py --inventory $(MEASURE)

measure-coupling: ## Print the coupling and cohesion of src/factory, from its imports and its git history
	$(RUN) python tools/measure/coupling.py

# Cleanup

clean: ## Remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist
	find . -path ./.venv -prune -o -name __pycache__ -type d -exec rm -rf {} +

distclean: clean ## Remove caches and .venv
	rm -rf .venv
