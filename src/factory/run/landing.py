"""Landing is the end of a Run that merged a Ticket: the text of each PR the Landing plans, the push of its head and
the PR.

The Run Stage `describe` has the Describer write the text, and the Run node `open_pr` opens the PR with that text, or
with the Tracker's own when the Run graph has no such Stage or the Stage failed.
`PullRequests` opens the PR, after the push of its branch when it lands through a git remote.
"""

import json
import re
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from factory.core.events import Event, applies, measures, times
from factory.core.git import GitError, HostGit
from factory.core.names import abandoned_ref
from factory.core.run_agent import DESCRIBER
from factory.core.states import TicketState
from factory.journal.artifacts import write_json_once, write_once
from factory.journal.metrics_memory import run_stage_ended, run_stage_started
from factory.journal.run_state import RunMemory
from factory.journal.timeline_memory import TimelineMemory
from factory.pipeline.pipeline import Role, RunStage
from factory.ports.executor import AttemptResult
from factory.ports.tracker import PullRequest, PullRequests, SpecSnapshot, TrackerError
from factory.run.inputs import schema_errors
from factory.run.landings import Landing, PlannedPr, landing_remote
from factory.run.run_agents import RunAgents
from factory.run.run_context import Append, RunContext


def describer_names(home: Path, run_id: str) -> tuple[str, str, Path]:
    """Returns the Sandbox, branch and Workspace of the Describer, none of which a Ticket's can equal."""
    return DESCRIBER.names(home, run_id)


def push_command(remote: str | None, pull_requests: PullRequests, pr: PlannedPr) -> list[str]:
    """Returns the command that pushes the head of a PR by hand, to the remote when the landing found it."""
    return ["git", "push", remote or pull_requests.url(), pr.head]


class Lander:
    """Lander has the Describer write the text of each PR the Landing plans, pushes their heads and opens them."""

    def __init__(
        self,
        context: RunContext,
        append: Append,
        landing: Landing,
        git: HostGit,
        pull_requests: PullRequests,
        run_agents: RunAgents,
    ) -> None:
        self.append = append
        self.landing = landing
        self.git = git
        self.run_agents = run_agents
        self.pull_requests = pull_requests
        self.context = context

    @property
    def outcome(self) -> str:
        """Returns the outcome of the `open_pr` node: `opened` with the Run's final PR open, and `failed` when no Ticket
        merged or a step of the landing failed."""
        return "opened" if self.context.memory.pull_request else "failed"

    def describe(self, stage: RunStage) -> str:
        """Has the Describer write the text of each PR the Run opens, and returns the outcome of the Stage.

        It ends `done` when the Describer wrote them all, also when a Run that resumes wrote them before, and `failed`
        when one has no text or no Ticket merged.
        The Journal names the file of each text it wrote, which `open_pr` reads even after a kill.
        """
        if self.context.memory.pull_request:
            return "done"
        if not any(t.state == TicketState.MERGED for t in self.context.tickets):
            return "failed"
        return "done" if all([self._describe(stage, pr) for pr in self.landing.plan()]) else "failed"

    def open_pr(self) -> None:
        """Pushes the head of each PR the Landing plans and opens it, bottom up, for a Run that merged a Ticket.

        The text is an Artifact in any case, and PullRequests that push nothing record the PR without a push.
        A PR whose head the Journal holds as opened is skipped, so a Run that resumes opens the rest.
        A step that fails ends the landing and leaves the Run's state alone, and `LandingFailed` journals its reason
        with the commands that finish the landing by hand, which the Run's report prints.
        """
        memory = self.context.memory
        if memory.pull_request or not any(t.state == TicketState.MERGED for t in self.context.tickets):
            return
        plan = self.landing.plan()
        pull_requests = self.pull_requests
        by_hand: list[list[str]] = []
        failed: dict[str, str] = {}
        remote: str | None = None
        for pr in plan:
            if pr.head in memory.opened:
                continue
            pull = self._pull_request(pr, plan)
            suffix = "" if pr.top else f"-{pr.number}"
            body = write_once(self.context.run_dir.root / f"pull_request{suffix}.md", pull.body)
            write_once(self.context.run_dir.root / f"pull_request_title{suffix}.txt", f"{pull.title}\n")
            layer = pr.number or None
            if not pull_requests.pushes:
                self._ensure_head(pr)
                path = pull_requests.open(pr.head, pr.base, pull.title, body, layer)
                self.append(Event.PullRequestRecorded, {"path": path, "head": pr.head, "base": pr.base, "top": pr.top})
                continue
            open_command = pull_requests.open_command(pr.head, pr.base, pull.title, body)
            if failed:
                by_hand += [push_command(remote, pull_requests, pr), open_command]
                continue
            try:
                self._ensure_head(pr)
                remote = landing_remote(self.git, pull_requests)
                self.git.push(remote, pr.head)
            except GitError as error:
                failed = {"step": "push", "reason": str(error)}
                by_hand += [push_command(remote, pull_requests, pr), open_command]
                continue
            self.append(Event.IntegrationBranchPushed, {"remote": remote, "branch": pr.head, "commit": pr.commit})
            try:
                url = pull_requests.open(pr.head, pr.base, pull.title, body, layer)
            except TrackerError as error:
                failed = {"step": "pull_request", "reason": str(error)}
                by_hand.append(open_command)
                continue
            payload = {"url": url, "head": pr.head, "base": pr.base, "body": str(body), "top": pr.top}
            self.append(Event.PullRequestOpened, payload)
        if failed:
            self.append(Event.LandingFailed, {**failed, "commands": by_hand})

    def _ensure_head(self, pr: PlannedPr) -> None:
        """Creates the head branch of a PR at its commit, and leaves it alone when it is there already, as the
        Integration branch is and as a Run that resumes left a layer's branch."""
        try:
            if self.git.resolve(pr.head) == pr.commit:
                return
        except GitError:
            pass
        self.git.create_branch(pr.head, pr.commit)

    def _pull_request(self, pr: PlannedPr, plan: list[PlannedPr]) -> PullRequest:
        """Returns the text of a PR: the Describer's, or the Tracker's own, then the footer that names its Tickets, the
        notes of the Landing and, on the top PR, the issues that `file_issues` filed."""
        self._ensure_head(pr)
        ids = {t.id for t in pr.tickets}
        snapshot = self.context.snapshot
        own = SpecSnapshot(snapshot.ref, snapshot.title, snapshot.body, [t for t in snapshot.tickets if t.id in ids])
        states = {t.id: t.state for t in pr.tickets}
        kept = {t.id: abandoned_ref(self.context.run_id, t.id) for t in pr.tickets if t.kept}
        fallback = self.context.closing.describe(own, self.context.run_id, states, kept)
        footer = self.context.closing.footer(own, states, kept)
        pull = self._text(fallback, footer, pr)
        extras = [self.landing.notes(pr, plan), self._filed_issues() if pr.top else ""]
        if not any(extras):
            return pull
        return replace(pull, body="\n\n".join([pull.body.rstrip(), *(extra for extra in extras if extra)]))

    def _filed_issues(self) -> str:
        """Lists the issues `file_issues` filed or reused, per Ticket, or returns an empty text when it filed none."""
        lines = []
        for ticket in self.context.tickets:
            for issue in ticket.issues:
                how = "reused" if issue["reused"] else "filed"
                reference = self.context.closing.reference(issue["id"])
                lines.append(f"- Ticket {ticket.id}: {reference} {issue['title']} ({how})\n")
        return "Issues:\n\n" + "".join(lines) if lines else ""

    def _text(self, fallback: PullRequest, footer: str, pr: PlannedPr) -> PullRequest:
        """Returns the PR text, which is the Describer's when it wrote one for the PR, or else `fallback`.

        The engine appends its own `footer` to the Describer's body, so closing the Tickets
        does not depend on the agent.
        The Tracker defuses the closing keywords in that body, so only the footer closes issues.
        """
        path = self.context.memory.described.get(pr.number)
        if path is None:
            return fallback
        output = json.loads(Path(path).read_text())
        body = f"{self.context.closing.defuse(str(output['body'])).strip()}\n\n{footer}"
        return PullRequest(title=str(output["title"]), body=body)

    def _describe(self, stage: RunStage, pr: PlannedPr) -> bool:
        """Runs the Attempts of the Describer until one writes the text of `pr`, and tells whether the PR has one.

        A Describer that fails every Attempt is journaled with the reason of each.
        The Describer reads the diff of the PR's head against its base.
        """
        if pr.number in self.context.memory.described:
            return True
        self._ensure_head(pr)
        for attempt in range(1, stage.settings.attempts + 1):
            begun = self.run_agents.ports.clock.now()
            result, attempt_dir = self._describe_once(stage, attempt, pr)
            problems = self._pull_request_problems(stage.role, result)
            spent = {
                "ticket": DESCRIBER.slot,
                "node": stage.name,
                "round": pr.number or 1,
                "attempt": attempt,
                "duration": (self.run_agents.ports.clock.now() - begun).total_seconds(),
                "usage": asdict(result.usage) if result.usage else None,
                "stats": asdict(result.stats) if result.stats else None,
            }
            if not problems:
                assert result.output is not None
                output = write_json_once(attempt_dir / "output.json", result.output)
                described = {**spent, "outcome": "done", "title": result.output["title"], "output": str(output)}
                self.append(Event.PullRequestDescribed, {**described, "layer": pr.number} if pr.number else described)
                return True
            cause = result.failure or "invalid_output"
            silent = {"silent": result.silent} if result.silent is not None else {}
            failed = {**spent, "outcome": "failed", "cause": cause, "reason": problems, **silent}
            self.append(Event.DescriberFailed, failed)
        return False

    def _pull_request_problems(self, role: Role, result: AttemptResult) -> str:
        """Tells why the Describer's result is no PR text, or returns an empty text when it is one.

        Every PR needs output on the Role's schema and a title of one line, and the Role adds the headings and words
        its body must hold.
        """
        if result.failure is not None:
            return result.reason or result.failure
        errors = schema_errors(role.output, result.output)
        if errors or result.output is None:
            return "; ".join(errors)
        title = str(result.output["title"])
        if not title.strip() or title != title.strip() or "\n" in title or "\r" in title:
            return "the title must be one line with no space around it"
        body = str(result.output["body"])
        headings, words = role.body.headings, role.body.words
        missing = [name for name in headings if not re.search(rf"^#+\s+{re.escape(name)}\s*$", body, re.MULTILINE)]
        missing += [name for name in words if name not in body]
        return f"the body lacks {', '.join(missing)}" if missing else ""

    def _describe_once(self, stage: RunStage, attempt: int, pr: PlannedPr) -> tuple[AttemptResult, Path]:
        """Runs one Attempt of the Describer in a Workspace on a branch at the PR's commit, and discards its changes.

        The Attempts of a layer of a stack have a directory of their own, and the directory of the Attempt comes back
        with its result.
        A Sandbox or git step that fails ends the Attempt as `sandbox_start`.
        """
        layer_dir = f"layer-{pr.number}" if pr.number else ""
        attempt_dir = self.context.run_dir.run_agent(DESCRIBER.name) / layer_dir / f"attempt-{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        work = stage.agent
        started = {
            "ticket": DESCRIBER.slot,
            "node": stage.name,
            "round": pr.number or 1,
            "attempt": attempt,
            **self.run_agents.attempts.variant.of(work, work.settings),
            "head": pr.commit,
        }
        result = self.run_agents.apart(
            DESCRIBER,
            pr.commit,
            attempt_dir,
            lambda: self.run_agents.work_apart(
                DESCRIBER,
                work,
                attempt_dir,
                self._describer_brief(stage, attempt, pr, attempt_dir),
                DESCRIBER.started,
                started,
            ),
            lambda reason: AttemptResult(output=None, failure="sandbox_start", reason=reason),
        )
        return result, attempt_dir

    def _describer_brief(self, stage: RunStage, attempt: int, pr: PlannedPr, attempt_dir: Path) -> dict[str, Any]:
        """Builds the Describer's Brief from the Spec, the merged Tickets of the PR with their Gate verdicts and Stage
        outputs, the PR's base and its head, which the Brief calls the Integration branch."""
        inputs: list[dict[str, Any]] = [{"source": "spec", "path": str(self.context.run_dir.spec)}]
        for ticket in pr.tickets:
            if ticket.state != TicketState.MERGED:
                continue
            inputs.append(
                {"source": "ticket", "ticket": ticket.id, "path": str(self.context.run_dir.ticket(ticket.id))}
            )
            for artifact in ticket.artifacts:
                entry = {k: v for k, v in artifact.items() if k != "kind"}
                inputs.append({"source": artifact["kind"], "ticket": ticket.id, **entry})
        return {
            "run": self.context.run_id,
            "stage": stage.name,
            "attempt": attempt,
            "base": pr.base_commit,
            "base_ref": pr.base,
            "integration_branch": pr.head,
            **({"layer": pr.number} if pr.number else {}),
            "inputs": inputs,
            "output_dir": str(attempt_dir),
        }


# What the events of the landing tell the readers of the Journal.


@applies(Event.LandingFailed)
def _apply_landing_failed(memory: RunMemory, payload: dict[str, Any]) -> None:
    """Keeps the commands that finish the landing by hand, which a Journal from before they were journaled lacks."""
    memory.by_hand = payload.get("commands", [])


@applies(Event.PullRequestDescribed)
def _apply_pull_request_described(memory: RunMemory, payload: dict[str, Any]) -> None:
    """Keeps where the text is, which a Journal from when the node `land` wrote the PR at once does not tell."""
    if "output" in payload:
        memory.described[payload.get("layer", 0)] = payload["output"]


measures(Event.DescriberStarted)(run_stage_started)
measures(Event.PullRequestDescribed, Event.DescriberFailed)(run_stage_ended)


@applies(Event.IntegrationBranchPushed)
def _apply_integration_branch_pushed(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.pushed = payload["commit"]


@applies(Event.PullRequestOpened)
def _apply_pull_request_opened(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.opened.append(payload["head"])
    if payload.get("top", True):
        memory.pull_request, memory.by_hand = payload["url"], []


@applies(Event.PullRequestRecorded)
def _apply_pull_request_recorded(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.opened.append(payload["head"])
    if payload.get("top", True):
        memory.pull_request, memory.by_hand = payload["path"], []


@times(Event.PullRequestOpened)
def _time_pull_request_opened(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    memory.line("pr", "opened", payload["url"])
