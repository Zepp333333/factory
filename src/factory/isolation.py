"""The isolation module keeps the Sandboxes, Workspaces and branches that Attempts, Probes and Operations use.

It lists what a Ticket's Sandbox sees, reaps the processes an earlier Attempt or Probe left in it,
counts the Sandboxes a Ticket lost against `sandbox_retries`, and removes the Sandbox, the Workspace and the branch
once nothing uses them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.artifacts import RunDir
from factory.config import Project
from factory.events import Event, applies, nothing
from factory.git import GitError, HostGit
from factory.live import LiveTicket
from factory.pipeline import Pipeline
from factory.run_context import Ports
from factory.sandbox import Mount, SandboxError
from factory.workbench import WorkbenchError


@dataclass(frozen=True)
class CleanupEvents:
    """CleanupEvents say how a clean-up journals each step it ends."""

    removed: Callable[[], None]
    left_behind: Callable[[str], None]
    workspace_removed: Callable[[], None]
    branch_deleted: Callable[[], None]
    failed: Callable[[str, Exception], None]


class Isolation:
    """Isolation reaps, counts and removes the Sandboxes, Workspaces and branches of a Run.

    `append` journals an event and applies it to the Run state.
    """

    def __init__(
        self,
        append: Callable[[Event, dict[str, Any]], None],
        project: Project,
        pipeline: Pipeline,
        ports: Ports,
        git: HostGit,
    ) -> None:
        self.append = append
        self.project = project
        self.pipeline = pipeline
        self.ports = ports
        self.git = git

    def mounts(self, ticket: LiveTicket, run_dir: RunDir) -> list[Mount]:
        """Lists what the Ticket's Sandbox sees besides its Workspace.

        Git in the Sandbox reads the shared `.git` and never writes it, because the host commits (ADR 0009).
        The Ticket's directory is writable, so its Attempts and Probes can leave their exit codes and reports.
        Its earlier Artifacts are read-only files there, but a process in the Sandbox can still delete or replace them,
        and only the Spec and the Ticket's text are mounted read-only.
        """
        assert ticket.workspace is not None
        run_dir.agent_home.mkdir(exist_ok=True)
        return [
            Mount(self.git.common),
            Mount(run_dir.ticket_dir(ticket.id), writable=True),
            Mount(run_dir.spec),
            Mount(run_dir.ticket(ticket.id)),
            Mount(run_dir.agent_home, writable=True),
            Mount(run_dir.kit),
            *(Mount(path) for path in self.ports.executor.reads()),
        ]

    def reap(self, ticket: LiveTicket, sandbox: str) -> None:
        """Kills the processes that earlier Attempts and Probes left in the Ticket's Sandbox and journals each one.

        It runs before the next Attempt or Probe starts, so nothing of the current one can be hit.
        A Sandbox that cannot reap is journaled and the Attempt or Probe starts anyway.
        """
        try:
            reaped = self.ports.sandbox.reap(sandbox)
        except (SandboxError, OSError) as error:
            self.append(Event.SandboxReapFailed, {"ticket": ticket.id, "sandbox": sandbox, "reason": str(error)})
            return
        for process in reaped:
            self.append(
                Event.SandboxProcessKilled,
                {
                    "ticket": ticket.id,
                    "sandbox": sandbox,
                    "pid": process.pid,
                    "command": process.command,
                    "lifetime": process.lifetime,
                },
            )

    def sandbox_gone_too_often(self, ticket: LiveTicket, cause: str, reason: str | None) -> bool:
        """Tells whether the Ticket lost its Sandbox more often than `sandbox_retries`, and journals it if so.

        An Attempt or a Probe the Sandbox lost counts as a loss of the Ticket, and past the cap its node ends.
        """
        if ticket.sandbox_losses <= self.pipeline.sandbox_retries:
            return False
        self.append(
            Event.SandboxRetriesExhausted,
            {
                "ticket": ticket.id,
                "node": ticket.node,
                "losses": ticket.sandbox_losses,
                "sandbox_retries": self.pipeline.sandbox_retries,
                "cause": cause,
                "reason": reason,
            },
        )
        return True

    def clean_up(
        self,
        sandbox: str,
        workspace: Path | None,
        branch: str,
        commit: str,
        events: CleanupEvents,
        settle: Callable[[], None] | None = None,
    ) -> None:
        """Removes the Sandbox, the Workspace and the branch an Attempt or a Ticket used, in that order.

        `settle` runs first on a Workspace that is still there, and a `workspace` of None has none to remove.
        A step that fails is reported with its reason and keeps what the steps after it would remove.
        A Sandbox that stays behind stopped for good runs nothing more, so the Workspace and branch still go.
        The branch goes only while it still points at `commit`, so no later commit is lost with it.
        """
        try:
            left_behind = self.ports.sandbox.remove(sandbox)
        except (SandboxError, OSError) as error:
            events.failed("sandbox", error)
            return
        if left_behind is None:
            events.removed()
        else:
            events.left_behind(left_behind)
        if workspace is not None:
            try:
                if settle is not None:
                    settle()
                self.git.pin(workspace, branch)
                self.ports.workbench.remove_workspace(self.project.repo, workspace)
            except (GitError, WorkbenchError, OSError) as error:
                events.failed("workspace", error)
                return
            events.workspace_removed()
        try:
            self.git.delete_branch(branch, commit)
        except GitError as error:
            events.failed("branch", error)
            return
        events.branch_deleted()


# What the events of a Sandbox's reaping tell the readers of the Journal.

applies(Event.SandboxProcessKilled, Event.SandboxReapFailed, Event.SandboxRetriesExhausted)(nothing)
