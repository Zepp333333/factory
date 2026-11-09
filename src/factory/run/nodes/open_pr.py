"""The kind `open_pr` pushes the Integration branch and opens the Run's PR."""

from factory.pipeline.pipeline import OPEN_PR, TICKETS
from factory.pipeline.run_kinds import Order, RunNodeKind, fixed

KIND = RunNodeKind(name=OPEN_PR, outcomes=fixed("opened", "failed"), order=Order(after=lambda node: (TICKETS,)))
