"""RunAgents runs the Attempts of the Run-level agents, which no Ticket owns, as the Describer and the CI fixer.

A RunAgent works in a Workspace and a Sandbox of its own on a branch at the Integration branch's tip, waits for a slot
under its own name before each Attempt, and leaves nothing behind: its Sandbox, Workspace and branch go after its work,
and what is left uncommitted goes to `discarded.patch`.
What it does there is its own module's: `landing` for the Describer and `ci_fix` for the CI fixer.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from factory.artifacts import write_json_once, write_once
from factory.events import Event
from factory.executor import AttemptResult
from factory.git import GitError
from factory.isolation import CleanupEvents
from factory.pipeline import AgentWork
from factory.run_agent import RunAgent
from factory.run_context import RunContext
from factory.run_tools import RunTools
from factory.sandbox import Mount, SandboxError
from factory.workbench import WorkbenchError

# The errors of a Sandbox, Workspace or git step, which end a RunAgent's work as `sandbox_start`.
APART_ERRORS = (SandboxError, WorkbenchError, GitError, OSError)


class RunAgents:
    """RunAgents gives a RunAgent its Workspace and Sandbox, runs its Attempts there and cleans up after it."""

    def __init__(self, context: RunContext, tools: RunTools) -> None:
        self.append = tools.append
        self.project = tools.project
        self.ports = tools.ports
        self.home = tools.home
        self.git = tools.git
        self.attempts = tools.attempts
        self.context = context

    def names(self, agent: RunAgent) -> tuple[str, str, Path]:
        """Returns the agent's Sandbox, branch and Workspace in this Run."""
        return agent.names(self.home, self.context.run_id)

    def apart[T](
        self,
        agent: RunAgent,
        tip: str,
        settle_dir: Path,
        work: Callable[[], T],
        failed: Callable[[str], T],
    ) -> T:
        """Does the agent's `work` in its Workspace and Sandbox on a branch at `tip`, and returns what it returns.

        A Sandbox, Workspace or git step that fails returns what `failed` makes of its reason.
        The Sandbox, the Workspace and the branch are removed after it, and what is left uncommitted goes to
        `discarded.patch` in `settle_dir`.
        """
        sandbox, branch, workspace = self.names(agent)
        try:
            self.git.create_branch(branch, tip)
            self.ports.workbench.create_workspace(self.project.repo, workspace, branch)
            self.ports.sandbox.create(sandbox, workspace, self._mounts(agent))
            result = work()
        except APART_ERRORS as error:
            result = failed(str(error))
        self._clean_up(agent, settle_dir)
        return result

    def work_apart(
        self,
        agent: RunAgent,
        work: AgentWork,
        attempt_dir: Path,
        brief: dict[str, Any],
        event: Event,
        started: dict[str, Any],
    ) -> AttemptResult:
        """Runs an Attempt of the agent in its Sandbox and returns its result.

        It writes the Brief, waits for a slot under the agent's name, takes it and journals `event` with `started` and
        the Brief in one transaction, and waits for the agent.
        The caller journals the event that ends the Attempt and gives the slot back.
        """
        sandbox, _, workspace = self.names(agent)
        brief_path = write_json_once(attempt_dir / "brief.json", brief)
        request = self.attempts.request(work, work.settings, brief_path, attempt_dir, sandbox, workspace)
        payload = {**started, "brief_path": str(brief_path), "brief": brief}
        while True:
            with self.attempts.journal.transaction():
                if self.attempts.take_slot(agent.slot):
                    self.append(event, payload)
                    break
            self.ports.clock.sleep(self.attempts.tick)
            self.attempts.beat()
        return self.attempts.run_apart(work, request)

    def _mounts(self, agent: RunAgent) -> list[Mount]:
        """Lists what the agent's Sandbox sees besides its Workspace: the Run's Artifacts, read-only, and the agent's
        own directory of them, writable.
        """
        run_dir = self.context.run_dir
        run_dir.agent_home.mkdir(exist_ok=True)
        return [
            Mount(self.git.common),
            Mount(run_dir.spec),
            Mount(run_dir.root / "tickets"),
            Mount(run_dir.run_agent(agent.name), writable=True),
            Mount(run_dir.agent_home, writable=True),
            Mount(run_dir.kit),
            *(Mount(path) for path in self.ports.executor.reads()),
        ]

    def _clean_up(self, agent: RunAgent, settle_dir: Path) -> None:
        """Removes what the agent's work used, and keeps what is left uncommitted in `discarded.patch`.

        A branch that was never made leaves nothing to remove.
        Each step that ends is journaled with the agent's events, and a step that fails is journaled with its reason and
        leaves the steps after it undone.
        """
        sandbox, branch, workspace = self.names(agent)
        try:
            head = self.git.resolve(branch)
        except GitError:
            return

        def settle() -> None:
            patch, _ = self.git.discard(workspace, branch, head)
            if patch:
                write_once(settle_dir / "discarded.patch", patch)

        def noop() -> None:
            pass

        events = CleanupEvents(
            removed=lambda: self.append(agent.sandbox_removed, {"sandbox": sandbox}),
            left_behind=lambda reason: self.append(agent.sandbox_left_behind, {"sandbox": sandbox, "reason": reason}),
            workspace_removed=noop,
            branch_deleted=noop,
            failed=lambda step, error: self.append(agent.cleanup_failed, {"reason": str(error)}),
        )
        self.attempts.isolation.clean_up(
            sandbox, workspace if workspace.exists() else None, branch, head, events, settle
        )
