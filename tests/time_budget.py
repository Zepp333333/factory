"""Sums the time of every test outside `tests/contract/` and, with `--time-budget`, fails the run past the budget.

The time of a test is its setup, call and teardown, as the junit report of `make measure-durations` counts it.

The budget is the ini option `time_budget` in `pyproject.toml`.
Contract tests run only with `FACTORY_CONTRACT=1` and wait on real services, so they stay out of the sum.
Under `pytest-xdist` the controller receives every report, so only the controller sums and judges.
"""

import pytest

CONTRACT = "tests/contract/"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini("time_budget", "seconds that the tests outside tests/contract may take together", default="")
    parser.addoption(
        "--time-budget",
        action="store_true",
        dest="enforce_time_budget",
        help="fail the run when the summed test time passes the ini option time_budget",
    )


def pytest_configure(config: pytest.Config) -> None:
    if not hasattr(config, "workerinput"):
        config.pluginmanager.register(TimeBudget(config), "time-budget")


class TimeBudget:
    def __init__(self, config: pytest.Config) -> None:
        budget = config.getini("time_budget")
        self.enforced: bool = config.getoption("enforce_time_budget")
        if self.enforced and not budget:
            raise pytest.UsageError("--time-budget needs the ini option time_budget")
        self.budget = float(budget or "inf")
        self.summed = 0.0

    @property
    def over(self) -> bool:
        return self.enforced and self.summed > self.budget

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if not report.nodeid.startswith(CONTRACT):
            self.summed += report.duration

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if self.over and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter: pytest.TerminalReporter) -> None:
        line = f"summed test time {self.summed:.1f} s"
        if self.over:
            terminalreporter.write_line(f"{line}, over the budget of {self.budget:g} s", red=True)
        elif self.budget != float("inf"):
            terminalreporter.write_line(f"{line} of the budget of {self.budget:g} s")
