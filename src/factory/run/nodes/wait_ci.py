"""The kind `wait_ci` waits for the CI of the Run's PR."""

from factory.pipeline.pipeline import WAIT_CI
from factory.pipeline.run_kinds import WAIT_SHAPE, RunNodeKind, fixed

KIND = RunNodeKind(name=WAIT_CI, outcomes=fixed("green", "red", "absent", "timeout"), shape=WAIT_SHAPE)
