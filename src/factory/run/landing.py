"""Landing is the end of a Run that merged a Ticket: the PR text, the push of the Integration branch and the PR.

The Run Stage `describe` has the Describer write the text, and the Run node `open_pr` opens the PR with that text, or
with the Tracker's own when the Run graph has no such Stage or the Stage failed.
`PullRequests` opens the PR, after the push of its branch when it lands through a git remote.
The Planner, the other Run Stage, works at the start of a Run, and lives here because it needs nothing that the
Describer does not, and a module of its own would add edges past the ceiling of `tests/test_layers.py`.
"""

import json
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from factory.config import Project
from factory.core.events import Event, applies, measures, nothing, times
from factory.core.git import GitError, HostGit
from factory.core.names import abandoned_ref, layer_branch
from factory.core.run_agent import DESCRIBER, PLANNER
from factory.core.states import SANDBOX_FAILURES, TicketState
from factory.journal.artifacts import write_json_once, write_once
from factory.journal.metrics_memory import FINGERPRINT, MetricsMemory, attempt_metrics, spend
from factory.journal.run_state import RunMemory
from factory.journal.timeline_memory import TimelineMemory
from factory.pipeline.pipeline import RETRY, Role, RunNode, RunStage
from factory.ports.executor import AttemptResult
from factory.ports.tracker import PullRequest, PullRequests, SpecSnapshot, TrackerError
from factory.run.inputs import schema_errors
from factory.run.live import LiveTicket
from factory.run.run_agents import RunAgents
from factory.run.run_context import Append, RunContext


def describer_names(home: Path, run_id: str) -> tuple[str, str, Path]:
    """Returns the Sandbox, branch and Workspace of the Describer, none of which a Ticket's can equal."""
    return DESCRIBER.names(home, run_id)


def landing_remote(git: HostGit, pull_requests: PullRequests) -> str:
    """Returns the first git remote, by name, that points at the repository the PR opens in."""
    for name, url in sorted(git.remotes().items()):
        if pull_requests.accepts(url):
            return name
    raise GitError(f"no git remote of {git.repo} points at {pull_requests.url()}")


@dataclass(frozen=True)
class StackLayer:
    """A StackLayer is one PR of a stacked Run: the Ticket it lands, its `branch` at `commit`, and the branch and commit
    of the layer below, which are the base ref and the Run's base commit for the first one.
    """

    number: int
    ticket: LiveTicket
    title: str
    branch: str
    commit: str
    below: str
    below_commit: str


def push_command(remote: str | None, pull_requests: PullRequests, layer: StackLayer) -> list[str]:
    """Returns the command that pushes a layer's branch by hand, to the remote when the landing found it."""
    return ["git", "push", remote or pull_requests.url(), layer.branch]


class Lander:
    """Lander has the Describer write the PR text of a Run, pushes its Integration branch and opens its PR."""

    def __init__(
        self,
        context: RunContext,
        append: Append,
        project: Project,
        git: HostGit,
        pull_requests: PullRequests,
        run_agents: RunAgents,
    ) -> None:
        self.append = append
        self.project = project
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
        layers = self._layers() if self.project.landing == "stack" else [None]
        return "done" if all([self._describe(stage, layer) for layer in layers]) else "failed"

    def open_pr(self) -> list[list[str]]:
        """Pushes the Integration branch of a Run that merged a Ticket and opens its PR.

        The text is an Artifact in any case, and PullRequests that push nothing record the PR without a push.
        A Run that resumes after its PR opened lands nothing again.
        A step that fails is journaled with its reason and leaves the Run's state alone,
        and `open_pr` then returns the commands that finish the landing by hand.
        """
        if self.context.memory.pull_request or not any(t.state == TicketState.MERGED for t in self.context.tickets):
            return []
        if self.project.landing == "stack":
            return self._land_stack()
        states = {t.id: t.state for t in self.context.tickets}
        kept = {t.id: abandoned_ref(self.context.run_id, t.id) for t in self.context.tickets if t.kept}
        fallback = self.context.closing.describe(self.context.snapshot, self.context.run_id, states, kept)
        footer = self.context.closing.footer(self.context.snapshot, states, kept)
        pull = self._text(fallback, footer)
        if filed := self._filed_issues():
            pull = replace(pull, body=f"{pull.body.rstrip()}\n\n{filed}")
        body = write_once(self.context.run_dir.pull_request, pull.body)
        write_once(self.context.run_dir.pull_request_title, f"{pull.title}\n")
        integration, base = self.context.integration, self.project.base
        pull_requests = self.pull_requests
        if not pull_requests.pushes:
            path = pull_requests.open(integration, base, pull.title, body)
            self.append(Event.PullRequestRecorded, {"path": path, "head": integration, "base": base})
            return []
        open_command = pull_requests.open_command(integration, base, pull.title, body)
        remote = None
        try:
            commit = self.git.resolve(integration)
            remote = landing_remote(self.git, pull_requests)
            self.git.push(remote, integration)
        except GitError as error:
            self.append(Event.LandingFailed, {"step": "push", "reason": str(error)})
            return [["git", "push", remote or pull_requests.url(), integration], open_command]
        self.append(Event.IntegrationBranchPushed, {"remote": remote, "branch": integration, "commit": commit})
        try:
            url = pull_requests.open(integration, base, pull.title, body)
        except TrackerError as error:
            self.append(Event.LandingFailed, {"step": "pull_request", "reason": str(error)})
            return [open_command]
        self.append(Event.PullRequestOpened, {"url": url, "head": integration, "base": base, "body": str(body)})
        return []

    def merge(self, node: RunNode) -> str | None:
        """Has the code host merge the Run's PR at the commit whose CI went green, and returns the node's outcome.

        It ends `merged` also for a PR that merged before, as one does for a Run killed right after its merge, and
        `refused` for what no retry changes, with the reason in the Journal.
        A Tracker that cannot be read returns None, because it may hold the merge, and the Run tries again.
        `factory check` makes sure the node comes after `opened` of an `open_pr` node, so the Run has its PR.
        """
        pr = self.context.memory.pull_request
        try:
            if self.pull_requests.pushes and self.pull_requests.merged(pr):
                return self._merged(node, pr, self.context.memory.ci_commit)
            why, commit = self._not_mergeable(pr)
            if why:
                return self._refused(node, pr, why)
            why = "the code host took the merge and did not merge the PR"
            try:
                self.pull_requests.merge(pr, commit, node.settings.method)
            except TrackerError as error:
                why = str(error)
            if self.pull_requests.merged(pr):
                return self._merged(node, pr, commit)
            return self._refused(node, pr, why)
        except (TrackerError, OSError) as error:
            self.append(
                Event.TrackerActionFailed,
                {"node": node.name, "target": pr, "action": "merge", "on_fail": RETRY, "reason": str(error)},
            )
            return None

    def _not_mergeable(self, pr: str) -> tuple[str, str]:
        """Returns why the Run's PR may not merge, or nothing and the commit whose CI lets it.

        Only CI that ended `passed` or `absent` lets it, and not the red CI an owner accepted, and the head must still
        be at the commit it ended on, as a Run with no CI at all takes the commit it pushed.
        """
        memory = self.context.memory
        if self.project.landing == "stack":
            return "a stack of PRs merges bottom up, by hand", ""
        if not self.pull_requests.pushes:
            return "the PR is a file, which no code host merges", ""
        if memory.ci is not None or memory.ci_ended not in ("", "passed", "absent"):
            return f"the CI of the PR is not green: {memory.ci.outcome if memory.ci else memory.ci_ended}", ""
        commit = memory.ci_commit or memory.pushed
        head = self.pull_requests.head(pr)
        if head != commit:
            return f"the head of the PR moved from {commit[:7]} to {head[:7]} after its CI", ""
        return "", commit

    def _merged(self, node: RunNode, pr: str, commit: str) -> str:
        self.append(
            Event.PullRequestMerged, {"node": node.name, "url": pr, "commit": commit, "method": node.settings.method}
        )
        return "merged"

    def _refused(self, node: RunNode, pr: str, reason: str) -> str:
        self.append(Event.PullRequestMergeRefused, {"node": node.name, "url": pr, "reason": reason})
        return "refused"

    def _layers(self) -> list[StackLayer]:
        """Returns the layers of the stack: one per merged Ticket, in the order the Integration branch merged them.

        Each branch is at the commit where its Ticket merged, and the top one is at the Integration branch's tip.
        """
        merged = [
            (ticket, landing) for landing in self.context.memory.landings if (ticket := self._ticket(landing.ticket))
        ]
        titles = {t.id: t.title for t in self.context.snapshot.tickets}
        layers: list[StackLayer] = []
        below, below_commit = self.project.base, self.context.base_commit
        for number, (ticket, landing) in enumerate(merged, 1):
            top = number == len(merged)
            commit = self.git.resolve(self.context.integration) if top else landing.after
            branch = layer_branch(self.context.run_id, number, titles[ticket.id])
            layers.append(StackLayer(number, ticket, titles[ticket.id], branch, commit, below, below_commit))
            below, below_commit = branch, commit
        return layers

    def _ticket(self, id: str) -> LiveTicket | None:
        return next((t for t in self.context.tickets if t.id == id and t.state == TicketState.MERGED), None)

    def _land_stack(self) -> list[list[str]]:
        """Lands the Run as a stack of PRs, one per merged Ticket, each into the branch of the layer below.

        The layer branches are made from commits the Integration branch already holds, bottom up.
        A PR that is open already is skipped, so a Run that resumes opens the rest.
        A step that fails is journaled and ends the landing, and `open_pr` returns the commands that finish it by hand.
        """
        layers = self._layers()
        by_hand: list[list[str]] = []
        remote: str | None = None
        for layer in layers:
            if layer.branch in self.context.memory.opened:
                continue
            top = layer is layers[-1]
            pull = self._layer_text(layer, layers)
            suffix = "" if top else f"-{layer.number}"
            body = write_once(self.context.run_dir.root / f"pull_request{suffix}.md", pull.body)
            write_once(self.context.run_dir.root / f"pull_request_title{suffix}.txt", f"{pull.title}\n")
            pull_requests = self.pull_requests
            if not pull_requests.pushes:
                self._ensure_branch(layer)
                path = pull_requests.open(layer.branch, layer.below, pull.title, body, layer.number)
                payload = {"path": path, "head": layer.branch, "base": layer.below, "top": top}
                self.append(Event.PullRequestRecorded, payload)
                continue
            open_command = pull_requests.open_command(layer.branch, layer.below, pull.title, body)
            if by_hand:
                by_hand += [push_command(remote, pull_requests, layer), open_command]
                continue
            try:
                self._ensure_branch(layer)
                remote = landing_remote(self.git, pull_requests)
                self.git.push(remote, layer.branch)
            except GitError as error:
                self.append(Event.LandingFailed, {"step": "push", "reason": str(error)})
                by_hand += [push_command(remote, pull_requests, layer), open_command]
                continue
            self.append(
                Event.IntegrationBranchPushed, {"remote": remote, "branch": layer.branch, "commit": layer.commit}
            )
            try:
                url = pull_requests.open(layer.branch, layer.below, pull.title, body, layer.number)
            except TrackerError as error:
                self.append(Event.LandingFailed, {"step": "pull_request", "reason": str(error)})
                by_hand.append(open_command)
                continue
            payload = {"url": url, "head": layer.branch, "base": layer.below, "body": str(body), "top": top}
            self.append(Event.PullRequestOpened, payload)
        return by_hand

    def _ensure_branch(self, layer: StackLayer) -> None:
        """Creates the branch of a layer at its commit, and leaves it alone when a Run that resumes made it."""
        try:
            if self.git.resolve(layer.branch) == layer.commit:
                return
        except GitError:
            pass
        self.git.create_branch(layer.branch, layer.commit)

    def _layer_text(self, layer: StackLayer, layers: list[StackLayer]) -> PullRequest:
        """Returns the text of one layer's PR: the Describer's from the diff against the layer below, then the
        footer that closes the layer's Ticket, the list of the stack and the words that ask for a bottom-up merge.

        The footer of the top layer also lists the issues that `file_issues` filed.
        """
        self._ensure_branch(layer)
        own = SpecSnapshot(
            self.context.snapshot.ref,
            self.context.snapshot.title,
            self.context.snapshot.body,
            [t for t in self.context.snapshot.tickets if t.id == layer.ticket.id],
        )
        alone = {layer.ticket.id: TicketState.MERGED}
        fallback = self.context.closing.describe(own, self.context.run_id, alone, {})
        footer = self.context.closing.footer(own, alone, {})
        pull = self._text(fallback, footer, layer)
        stack = "\n".join(
            f"{other.number}. {other.title} (`{other.branch}`)" + (" **<- this PR**" if other is layer else "")
            for other in layers
        )
        notes = (
            f"Stack of {len(layers)} PRs, from the bottom up:\n\n{stack}\n\n"
            f"Merge the stack bottom up, with a merge commit only (no squash, no rebase), "
            f"and delete the branch after each merge, so the code host retargets the next PR to `{self.project.base}`."
        )
        if layer is layers[-1] and (filed := self._filed_issues()):
            notes = f"{notes}\n\n{filed}"
        return replace(pull, body=f"{pull.body.rstrip()}\n\n{notes}")

    def _filed_issues(self) -> str:
        """Lists the issues `file_issues` filed or reused, per Ticket, or returns an empty text when it filed none."""
        lines = []
        for ticket in self.context.tickets:
            for issue in ticket.issues:
                how = "reused" if issue["reused"] else "filed"
                reference = self.context.closing.reference(issue["id"])
                lines.append(f"- Ticket {ticket.id}: {reference} {issue['title']} ({how})\n")
        return "Issues:\n\n" + "".join(lines) if lines else ""

    def _text(self, fallback: PullRequest, footer: str, layer: StackLayer | None = None) -> PullRequest:
        """Returns the PR text, which is the Describer's when it wrote one for the PR, or else `fallback`.

        The engine appends its own `footer` to the Describer's body, so closing the Tickets
        does not depend on the agent.
        The Tracker defuses the closing keywords in that body, so only the footer closes issues.
        """
        path = self.context.memory.described.get(layer.number if layer else 0)
        if path is None:
            return fallback
        output = json.loads(Path(path).read_text())
        body = f"{self.context.closing.defuse(str(output['body'])).strip()}\n\n{footer}"
        return PullRequest(title=str(output["title"]), body=body)

    def _describe(self, stage: RunStage, layer: StackLayer | None) -> bool:
        """Runs the Attempts of the Describer until one writes a text, and tells whether the PR has one.

        A Describer that fails every Attempt is journaled with the reason of each.
        The Describer of a `layer` reads its diff against the layer below.
        """
        number = layer.number if layer else 0
        if number in self.context.memory.described:
            return True
        if layer:
            self._ensure_branch(layer)
        for attempt in range(1, stage.settings.attempts + 1):
            tip = layer.commit if layer else self.git.resolve(self.context.integration)
            begun = self.run_agents.ports.clock.now()
            result, attempt_dir = self._describe_once(stage, attempt, tip, layer)
            problems = self._pull_request_problems(stage.role, result)
            spent = {
                "ticket": DESCRIBER.slot,
                "node": stage.name,
                "round": number or 1,
                "attempt": attempt,
                "duration": (self.run_agents.ports.clock.now() - begun).total_seconds(),
                "usage": asdict(result.usage) if result.usage else None,
                "stats": asdict(result.stats) if result.stats else None,
            }
            if not problems:
                assert result.output is not None
                output = write_json_once(attempt_dir / "output.json", result.output)
                described = {**spent, "outcome": "done", "title": result.output["title"], "output": str(output)}
                self.append(Event.PullRequestDescribed, {**described, "layer": layer.number} if layer else described)
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

    def _describe_once(
        self, stage: RunStage, attempt: int, tip: str, layer: StackLayer | None
    ) -> tuple[AttemptResult, Path]:
        """Runs one Attempt of the Describer in a Workspace on a branch at `tip`, and discards its changes.

        `tip` is the Integration branch's tip, or the commit of a layer of a stacked Run, whose Attempts have a
        directory of their own, and the directory of the Attempt comes back with its result.
        A Sandbox or git step that fails ends the Attempt as `sandbox_start`.
        """
        layer_dir = f"layer-{layer.number}" if layer else ""
        attempt_dir = self.context.run_dir.run_agent(DESCRIBER.name) / layer_dir / f"attempt-{attempt}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        work = stage.agent
        started = {
            "ticket": DESCRIBER.slot,
            "node": stage.name,
            "round": layer.number if layer else 1,
            "attempt": attempt,
            **self.run_agents.attempts.variant.of(work, work.settings),
            "head": tip,
        }
        result = self.run_agents.apart(
            DESCRIBER,
            tip,
            attempt_dir,
            lambda: self.run_agents.work_apart(
                DESCRIBER,
                work,
                attempt_dir,
                self._describer_brief(stage, attempt, layer, attempt_dir),
                DESCRIBER.started,
                started,
            ),
            lambda reason: AttemptResult(output=None, failure="sandbox_start", reason=reason),
        )
        return result, attempt_dir

    def _describer_brief(
        self, stage: RunStage, attempt: int, layer: StackLayer | None, attempt_dir: Path
    ) -> dict[str, Any]:
        """Builds the Describer's Brief from the Spec, the merged Tickets with their Gate verdicts and Stage outputs,
        the base ref and the Integration branch.

        The Brief of a layer names its Ticket alone, the layer below as the base ref and its own branch as the
        Integration branch.
        """
        inputs: list[dict[str, Any]] = [{"source": "spec", "path": str(self.context.run_dir.spec)}]
        for ticket in [layer.ticket] if layer else self.context.tickets:
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
            "base": layer.below_commit if layer else self.context.base_commit,
            "base_ref": layer.below if layer else self.project.base,
            "integration_branch": layer.branch if layer else self.context.integration,
            **({"layer": layer.number} if layer else {}),
            "inputs": inputs,
            "output_dir": str(attempt_dir),
        }


class Planner:
    """Planner has the Fresh agent of a Run Stage whose Role selects the Tickets say which of them the Run takes."""

    def __init__(self, context: RunContext, append: Append, run_agents: RunAgents) -> None:
        self.append = append
        self.run_agents = run_agents
        self.context = context

    def plan(self, stage: RunStage) -> Path | None:
        """Runs the Attempts of the Planner until one gives a plan, and returns the file of it, or None when none did.

        A Run that resumes after the Planner gave its output does not ask it again, and one that resumes while it worked
        cleans up after it, loses that Attempt without spending one, and goes on with the next.
        A failed Attempt spends one of `attempts`, and one whose Sandbox failed it counts against `sandbox_retries`.
        """
        memory = self.context.memory
        if memory.planned:
            return Path(memory.planned)
        agent_dir = self.context.run_dir.run_agent(PLANNER.name)
        # The directories count the Attempts, also one a kill lost before its start reached the Journal.
        attempt = len(list(agent_dir.glob("attempt-*")))
        self.run_agents.clean_up(PLANNER, agent_dir / f"attempt-{attempt}")
        while self._attempts_left(stage):
            attempt += 1
            attempt_dir = agent_dir / f"attempt-{attempt}"
            attempt_dir.mkdir()
            begun = self.run_agents.ports.clock.now()
            result = self._plan_once(stage, attempt, attempt_dir)
            problems = self._problems(stage.role, result)
            spent = {
                "ticket": PLANNER.slot,
                "node": stage.name,
                "round": 1,
                "attempt": attempt,
                "duration": (self.run_agents.ports.clock.now() - begun).total_seconds(),
                "usage": asdict(result.usage) if result.usage else None,
                "stats": asdict(result.stats) if result.stats else None,
            }
            if not problems:
                output = write_json_once(attempt_dir / "output.json", result.output)
                self.append(Event.PlannerFinished, {**spent, "outcome": "done", "output": str(output)})
                return output
            silent = {"silent": result.silent} if result.silent is not None else {}
            cause = result.failure or "invalid_output"
            self.append(
                Event.PlannerFailed, {**spent, "outcome": "failed", "cause": cause, "reason": problems, **silent}
            )
        return None

    def _attempts_left(self, stage: RunStage) -> bool:
        """Tells whether the Planner may start another Attempt, after what the Journal says the earlier ones spent."""
        failures = self.context.memory.plan_failures
        lost = sum(failure["cause"] in SANDBOX_FAILURES for failure in failures)
        return (
            len(failures) - lost < stage.settings.attempts and lost <= self.run_agents.project.pipeline.sandbox_retries
        )

    def _problems(self, role: Role, result: AttemptResult) -> str:
        """Tells why the result is no plan of the Tickets the Tracker read, or returns an empty text when it is one."""
        if result.failure is not None:
            return result.reason or result.failure
        errors = schema_errors(role.output, result.output)
        if errors or result.output is None:
            return "; ".join(errors)
        taken, left_out = result.output["tickets"], result.output["left_out"]
        if not _is_plan(taken, left_out):
            return "`tickets` must list Ticket ids and `left_out` must list objects with a `ticket` and a `reason`"
        return "; ".join(self.context.read.plan_problems(taken, [entry["ticket"] for entry in left_out]))

    def _plan_once(self, stage: RunStage, attempt: int, attempt_dir: Path) -> AttemptResult:
        """Runs one Attempt of the Planner in a Workspace on a branch at the Run's base, and discards its changes.

        A Sandbox or git step that fails ends the Attempt as `sandbox_start`.
        """
        work = stage.agent
        tip = self.context.base_commit
        started = {
            "ticket": PLANNER.slot,
            "node": stage.name,
            "round": 1,
            "attempt": attempt,
            **self.run_agents.attempts.variant.of(work, work.settings),
            "head": tip,
        }
        return self.run_agents.apart(
            PLANNER,
            tip,
            attempt_dir,
            lambda: self.run_agents.work_apart(
                PLANNER, work, attempt_dir, self._brief(stage, attempt, attempt_dir), PLANNER.started, started
            ),
            lambda reason: AttemptResult(output=None, failure="sandbox_start", reason=reason),
        )

    def _brief(self, stage: RunStage, attempt: int, attempt_dir: Path) -> dict[str, Any]:
        """Builds the Brief of the Planner from the Spec, the Tickets read and why its earlier Attempts failed."""
        run_dir = self.context.run_dir
        inputs: list[dict[str, Any]] = [{"source": "spec", "path": str(run_dir.spec)}]
        for ticket in self.context.read.tickets:
            inputs.append(
                {
                    "source": "ticket",
                    "ticket": ticket.id,
                    "path": str(run_dir.ticket(ticket.id)),
                    "title": ticket.title,
                    "blocked_by": [*ticket.blocked_by, *ticket.blocked_outside],
                    "labels": ticket.labels,
                }
            )
        for failure in self.context.memory.plan_failures:
            if failure["cause"] not in SANDBOX_FAILURES:
                inputs.append({"source": "cause", "attempt": failure["attempt"], "reason": failure["reason"]})
        return {
            "run": self.context.run_id,
            "stage": stage.name,
            "attempt": attempt,
            "base": self.context.base_commit,
            "inputs": inputs,
            "output_dir": str(attempt_dir),
        }


def _is_plan(taken: Any, left_out: Any) -> bool:
    """Tells whether the output has the shape the Run reads, which the schema of a project's own Role need not hold."""
    return (
        isinstance(taken, list)
        and all(isinstance(id, str) for id in taken)
        and isinstance(left_out, list)
        and all(
            isinstance(entry, dict) and isinstance(entry.get("ticket"), str) and isinstance(entry.get("reason"), str)
            for entry in left_out
        )
    )


# What the events of the landing and the Planner tell the readers of the Journal.

applies(Event.LandingFailed)(nothing)


@applies(Event.PlannerFinished)
def _apply_planner_finished(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.planned = payload["output"]


@applies(Event.PlannerFailed)
def _apply_planner_failed(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.plan_failures.append(payload)


@applies(Event.PullRequestDescribed)
def _apply_pull_request_described(memory: RunMemory, payload: dict[str, Any]) -> None:
    """Keeps where the text is, which a Journal from when the node `land` wrote the PR at once does not tell."""
    if "output" in payload:
        memory.described[payload.get("layer", 0)] = payload["output"]


@measures(Event.DescriberStarted, Event.PlannerStarted)
def _measure_run_stage_started(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    if (ticket := memory.ticket_of(payload)) is not None:
        key = (ticket.ticket, payload["node"], payload["round"], payload["attempt"])
        memory.fingerprints[key] = {name: payload.get(name) for name in FINGERPRINT}


@measures(Event.PullRequestDescribed, Event.DescriberFailed, Event.PlannerFinished, Event.PlannerFailed)
def _measure_run_stage_ended(memory: MetricsMemory, payload: dict[str, Any]) -> None:
    """Counts an Attempt of a Run Stage in the Stage rows of the Run, as a Journal from before Run Stages does not."""
    if (ticket := memory.ticket_of(payload)) is None:
        return
    key = (ticket.ticket, payload["node"], payload["round"], payload["attempt"])
    memory.attempts.append(attempt_metrics(ticket, payload, memory.fingerprints.get(key, {})))
    spend(ticket, payload)


@applies(Event.IntegrationBranchPushed)
def _apply_integration_branch_pushed(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.pushed = payload["commit"]


@applies(Event.PullRequestOpened)
def _apply_pull_request_opened(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.opened.append(payload["head"])
    memory.pull_request = payload["url"] if payload.get("top", True) else memory.pull_request


@applies(Event.PullRequestRecorded)
def _apply_pull_request_recorded(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.opened.append(payload["head"])
    memory.pull_request = payload["path"] if payload.get("top", True) else memory.pull_request


@applies(Event.PullRequestMerged)
def _apply_pull_request_merged(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.merge = "merged"


@applies(Event.PullRequestMergeRefused)
def _apply_pull_request_merge_refused(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.merge = payload["reason"]


@times(Event.PullRequestMerged)
def _time_pull_request_merged(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    memory.line("pr", "merged", payload["url"])


@times(Event.PullRequestMergeRefused)
def _time_pull_request_merge_refused(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    memory.line("pr", "merge refused", payload["reason"])


@times(Event.PullRequestOpened)
def _time_pull_request_opened(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    memory.line("pr", "opened", payload["url"])
