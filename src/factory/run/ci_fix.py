"""The CI fixer works the Rounds of a Run Stage with `workspace: integration` on red CI, and the Run Gates judge them.

The Addresser, such a Stage that takes `inputs`, works its Rounds the same way on the comments on the PR.

A Round is a Fresh agent in a Workspace and a Sandbox of its own on a branch at the Integration branch's tip, as
`run_agents` gives every RunAgent.
After the fixer the host commits its change (ADR 0009) and the CI guard judges the diff, and the Run Gates the Stage
routes to each judge that commit in a Sandbox of the Run.
A Round that passes them lands that commit where the Landing says, and a Round that fails leaves the branches alone
and gives the next Round its verdict.
"""

import re
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from factory.config import Project
from factory.core.events import Event, applies, nothing, times
from factory.core.git import GitError, HostGit
from factory.core.run_agent import CI_FIXER
from factory.journal.artifacts import ProbeDir, write_json_once, write_once
from factory.journal.commands import RUN_LEVEL
from factory.journal.run_state import RunMemory
from factory.journal.timeline_memory import TimelineMemory
from factory.pipeline import gates
from factory.pipeline.pipeline import Gate, RunStage
from factory.ports.executor import STREAM, AttemptResult
from factory.ports.run_ports import Ports
from factory.ports.tracker import PullRequests, TrackerError
from factory.run import ci_guard
from factory.run.inputs import schema_errors
from factory.run.landings import Landing
from factory.run.run_agents import APART_ERRORS, RunAgents
from factory.run.run_context import TICK_SECONDS, Append, RunContext


def one_line(text: object) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


class CiFixer:
    """CiFixer works the Rounds of the CI fix on the PR of a Run, and beats for the Run while it waits."""

    def __init__(
        self,
        context: RunContext,
        append: Append,
        project: Project,
        ports: Ports,
        git: HostGit,
        pull_requests: PullRequests,
        landing: Landing,
        beat: Callable[[], None],
        run_agents: RunAgents,
        read_inputs: Callable[[RunStage, set[str]], dict[str, list[dict[str, Any]]]],
    ) -> None:
        self.append = append
        self.project = project
        self.ports = ports
        self.git = git
        self.run_agents = run_agents
        self.pull_requests = pull_requests
        self.landing = landing
        self.beat = beat
        self.read_inputs = read_inputs
        self.tick = TICK_SECONDS
        self.context = context

    @property
    def _pending(self) -> dict[str, Any] | None:
        """Returns the Round whose commit the guard passed and that has not ended yet, or None when there is none."""
        fixes = self.context.memory.ci_fixes
        return fixes[-1] if fixes and "fixed" in fixes[-1] and not fixes[-1]["outcome"] else None

    def work(self, stage: RunStage) -> str:
        """Works one Round of the Stage on the red CI the Run waited for, and returns the Stage's outcome.

        It ends `exhausted` with no Round left or a budget spent, `failed` when the Round failed, and `done` when the
        guard passed its commit, which lands once the Round leaves the Gates.
        A Run killed with a Round in the agent's hands closes that Round first, and one killed with a commit the guard
        passed takes that commit up again.
        The report of a Round that failed goes to the final PR's body.
        `rounds` counts the Rounds of this Stage, and a Round reads what no pushed Round of it took from the Tracker.
        """
        memory = self.context.memory
        if self._pending is not None:
            return "done"
        fixes = memory.ci_fixes
        if fixes and not fixes[-1]["outcome"]:
            self.append(
                Event.CiFixFinished,
                {
                    "round": fixes[-1]["round"],
                    "outcome": "failed",
                    "cause": "lost",
                    "reason": "the Run stopped in this Round",
                },
            )
        own = [fix for fix in fixes if fix.get("node", stage.name) == stage.name]
        limit = stage.max_rounds
        if (limit is not None and len(own) >= limit) or memory.over_budget():
            return "exhausted"
        handled = {id for fix in own if fix["outcome"] == "pushed" for id in fix.get("comments", ())}
        heard = self.read_inputs(stage, handled)
        wait = memory.ci
        commit, jobs = (wait.commit, list(wait.jobs)) if wait else (memory.pushed, [])
        run_dir = self.context.run_dir
        number = len(fixes) + 1
        previous = own[-1] if own else None
        round_dir = run_dir.run_agent(CI_FIXER.name, number)
        failed_log = Path(wait.artifact).read_text() if wait and wait.artifact else ""
        log = write_once(round_dir / "failed.md", failed_log)
        tip = self.git.resolve(self.context.integration)
        self.append(
            Event.CiFixStarted,
            {
                "round": number,
                "node": stage.name,
                "commit": commit,
                "jobs": jobs,
                "artifact": str(log),
                "head": tip,
                **(
                    {"comments": [comment["id"] for said in heard.values() for comment in said]}
                    if stage.addresses
                    else {}
                ),
            },
        )
        self.run_agents.clear(CI_FIXER, round_dir / "leftover")
        report = self.run_agents.apart(
            CI_FIXER,
            tip,
            round_dir,
            lambda: self._attempts(stage, number, round_dir, tip, commit, jobs, log, previous, heard),
            lambda reason: {"outcome": "failed", "cause": "sandbox_start", "reason": reason},
        )
        if "fixed" in report:
            self.append(Event.CiFixCommitted, {"round": number, **report})
            return "done"
        self._finish_round(number, report)
        return "failed"

    def run_gate(self, gate: Gate) -> str:
        """Runs the Run Gate in a Sandbox of the Run at the commit of the Round, and returns its outcome.

        A Gate that fails ends the Round with its verdict, and the Integration branch stays where it was.
        A Run killed after that verdict finds no commit to judge and goes where the verdict led.
        """
        fix = self._pending
        if fix is None:
            return "fail"
        number, tip, fixed = fix["round"], fix["head"], fix["fixed"]
        round_dir = self.context.run_dir.run_agent(CI_FIXER.name, number)
        probe_dir, probe_number = self._probe_dir(round_dir, gate)
        self.run_agents.clear(CI_FIXER, probe_dir.root / "leftover")
        verdict = self.run_agents.apart(
            CI_FIXER,
            fixed,
            probe_dir.root,
            lambda: self._gate_verdict(gate, number, round_dir, probe_dir, probe_number, tip),
            lambda reason: {"cause": "sandbox_lost", "reason": reason},
        )
        if "cause" in verdict:
            self._fail_gate(number, round_dir, tip, fixed, verdict)
            return "fail"
        if verdict["outcome"] != "pass":
            reasons = verdict["reasons"]
            reason = f"Gate {gate.name} failed: {'; '.join(reasons)}" if reasons else f"Gate {gate.name} failed"
            self._fail_gate(number, round_dir, tip, fixed, {"cause": "gate", "reason": reason})
            return "fail"
        return "pass"

    def land(self) -> None:
        """Lands the commit of a Round that left its Gates where the Landing says, which pushes the head of the PR the
        Run waits on.

        A Run killed after a move moves nothing again, and a move or a push that fails ends the Round.
        Without such a Round it does nothing.
        """
        fix = self._pending
        if fix is None:
            return
        number, tip, fixed = fix["round"], fix["head"], fix["fixed"]
        try:
            remote, branch = self.landing.land_fix(fixed, tip)
        except GitError as error:
            self._finish_round(number, {"outcome": "failed", "cause": "push", "reason": str(error)})
            return
        self.append(Event.IntegrationBranchPushed, {"remote": remote, "branch": branch, "commit": fixed})
        self._finish_round(number, {"outcome": "pushed", "pushed": fixed})

    def _finish_round(self, number: int, report: dict[str, Any]) -> None:
        """Ends the Round with its report, which goes to the final PR's body whatever its outcome."""
        self.append(Event.CiFixFinished, {"round": number, **report})
        self._edit_pull_request(number, self.context.run_dir.run_agent(CI_FIXER.name, number))

    def _fail_gate(self, number: int, round_dir: Path, tip: str, fixed: str, why: dict[str, str]) -> None:
        write_once(round_dir / "fix.patch", self.git.diff(tip, fixed) + "\n")
        self._finish_round(number, {"outcome": "failed", **why})

    def _attempts(
        self,
        stage: RunStage,
        number: int,
        round_dir: Path,
        tip: str,
        commit: str,
        failed: list[dict[str, str]],
        log: Path,
        previous: dict[str, Any] | None,
        heard: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Starts Attempts of the fixer until one gives output or a budget is spent, then judges what it changed."""
        _, branch, workspace = self.run_agents.names(CI_FIXER)
        work = stage.agent
        report: dict[str, Any] = {"outcome": "failed", "cause": "no_attempt", "reason": "the fixer ran no Attempt"}
        for attempt in range(1, work.settings.attempts + 1):
            if attempt > 1 and self.context.memory.over_budget():
                break
            attempt_dir = round_dir / f"attempt-{attempt}"
            attempt_dir.mkdir(exist_ok=True)
            brief = self._brief(stage, number, attempt, attempt_dir, tip, commit, failed, log, previous, heard)
            started = {
                "ticket": CI_FIXER.slot,
                "node": stage.name,
                "round": number,
                "attempt": attempt,
                **self.run_agents.attempts.variant.of(work, work.settings),
                "head_before": tip,
                "stream": str(attempt_dir / STREAM),
            }
            begun = self.ports.clock.now()
            try:
                result = self.run_agents.work_apart(CI_FIXER, work, attempt_dir, brief, Event.AttemptStarted, started)
            except APART_ERRORS as error:
                result = AttemptResult(output=None, failure="sandbox_start", reason=str(error))
            spent = {
                "duration": (self.ports.clock.now() - begun).total_seconds(),
                "usage": asdict(result.usage) if result.usage else None,
                "stats": asdict(result.stats) if result.stats else None,
            }
            key = {"ticket": CI_FIXER.slot, "node": stage.name, "round": number, "attempt": attempt}
            errors = schema_errors(work.role.output, result.output)
            failure = result.failure or ("executor_crash" if errors else None)
            if failure is not None:
                discarded = self._discard(workspace, branch, tip, attempt_dir)
                record = {
                    "outcome": "failed",
                    "cause": failure,
                    **({"reason": result.reason} if result.reason else {}),
                    **spent,
                    "errors": errors,
                    "head_before": tip,
                    **discarded,
                }
                write_json_once(attempt_dir / "attempt.json", record)
                self.append(Event.AttemptFinished, {**key, **record})
                report = {"outcome": "failed", "cause": failure, "reason": result.reason or "; ".join(errors)}
                continue
            assert result.output is not None
            output = write_json_once(attempt_dir / "output.json", result.output)
            summary = str(result.output.get("summary") or "").strip().partition("\n")[0]
            trailers = {
                "Factory-Run": self.context.run_id,
                "Factory-Ticket": RUN_LEVEL,
                "Factory-Stage": stage.name,
                "Factory-Attempt": str(attempt),
            }
            subject = summary or f"{stage.name}: {self.context.run_id}"
            fixed = self.git.commit_all(workspace, branch, subject, trailers)
            record = {
                "outcome": "done",
                "output": str(output),
                "head_before": tip,
                "head_after": fixed or tip,
                **spent,
            }
            write_json_once(attempt_dir / "attempt.json", record)
            self.append(Event.AttemptFinished, {**key, **record})
            said: dict[str, str] = {
                "output": str(output),
                **{
                    field: one_line(result.output[field])
                    for field in ("failure", "change", "summary", "handoff")
                    if field in result.output
                },
            }
            return {**said, **self._guard(number, round_dir, tip, fixed)}
        return report

    def _guard(self, number: int, round_dir: Path, tip: str, fixed: str | None) -> dict[str, Any]:
        """Judges the fixer's commit by its diff, not the agent's word, which refuses a change of the CI config or a
        deleted, skipped or xfailed test, and returns the commit it passed or why the Round failed."""
        if fixed is None:
            return {"outcome": "failed", "cause": "no_change", "reason": "the fixer changed nothing"}
        found = ci_guard.problems(
            self.git.changed_files(tip, fixed), self.git.diff(tip, fixed), self.pull_requests.ci_config_paths
        )
        guard = write_json_once(
            round_dir / "guard.json", {"gate": "guard", "outcome": "fail" if found else "pass", "reasons": found}
        )
        self.append(
            Event.CiFixGateFinished,
            {
                "round": number,
                "node": "guard",
                "outcome": "fail" if found else "pass",
                "reasons": found,
                "verdict": str(guard),
            },
        )
        if found:
            write_once(round_dir / "fix.patch", self.git.diff(tip, fixed) + "\n")
            return {"outcome": "failed", "cause": "guard", "reason": "; ".join(found)}
        return {"fixed": fixed}

    def _probe_dir(self, round_dir: Path, gate: Gate) -> tuple[ProbeDir, int]:
        """Returns the directory of the Gate's next Probe in the Round and its number, so a Run killed in the Gate
        starts a new one.

        A Gate with `from` runs no Probe, and its directory names the latest Probe of its source Gate, which the graph
        check says came before it in the Round.
        """
        if gate.from_ is not None:
            number = len(list((round_dir / gate.from_).glob("probe-*")))
            return ProbeDir(round_dir / gate.name / f"from-{gate.from_}-probe-{number}"), number
        number = len(list((round_dir / gate.name).glob("probe-*"))) + 1
        return ProbeDir(round_dir / gate.name / f"probe-{number}"), number

    def _gate_verdict(
        self, gate: Gate, number: int, round_dir: Path, probe_dir: ProbeDir, probe_number: int, tip: str
    ) -> dict[str, Any]:
        """Runs the Gate in the Round's Sandbox on the fixer's commit, journals and returns its verdict.

        A Gate with `from` runs no Probe and reads Probe `probe_number` of its source Gate.
        A Run killed between such a verdict and its event judges the same Probe again and keeps the verdict it wrote.
        """
        sandbox, branch, workspace = self.run_agents.names(CI_FIXER)
        if gate.from_ is not None:
            source = ProbeDir(round_dir / gate.from_ / f"probe-{probe_number}")
            probe = gates.ProbeResult(
                gate_out=source.gate_out, log=source.log, exit_code=0, timed_out=False, duration=0
            )
            probe_dir.root.mkdir(parents=True, exist_ok=True)
        else:
            probe = self._probe(gate, probe_dir, sandbox, workspace, branch)
        if gates.KINDS[gate.kind].evaluates_changed_lines:
            probe = replace(probe, changed=self.git.changed_lines(workspace, branch, tip))
        verdict = gates.evaluate(gate.name, gate.kind, gate.with_, probe)
        path = probe_dir.verdict if probe_dir.verdict.exists() else write_json_once(probe_dir.verdict, verdict)
        self.append(
            Event.CiFixGateFinished,
            {
                "round": number,
                "node": gate.name,
                "probe": probe_number,
                **({"from": gate.from_} if gate.from_ is not None else {}),
                "outcome": verdict["outcome"],
                "reasons": verdict["reasons"],
                "verdict": str(path),
            },
        )
        return dict(verdict)

    def _probe(self, gate: Gate, probe_dir: ProbeDir, sandbox: str, workspace: Path, branch: str) -> gates.ProbeResult:
        """Runs the Probe of a Gate to its end while the Run beats, and kills it once it runs past its timeout."""
        self.git.reset(workspace, branch, ignored=gate.clean)
        probe_dir.gate_out.mkdir(parents=True)
        probe = gates.probe(gate.kind, gate.with_)
        process = self.ports.sandbox.exec(
            sandbox, probe.argv, stdout=probe_dir.log, env={"GATE_OUT": str(probe_dir.gate_out)}
        )
        started = self.ports.clock.now()
        while True:
            code = process.poll()
            elapsed = (self.ports.clock.now() - started).total_seconds()
            if code is not None or elapsed >= probe.timeout:
                break
            self.ports.clock.sleep(self.tick)
            self.beat()
        if code is None:
            process.kill()
        self.git.reset(workspace, branch, ignored=False)
        return gates.ProbeResult(
            gate_out=probe_dir.gate_out, log=probe_dir.log, exit_code=code, timed_out=code is None, duration=elapsed
        )

    def _brief(
        self,
        stage: RunStage,
        number: int,
        attempt: int,
        attempt_dir: Path,
        tip: str,
        commit: str,
        failed: list[dict[str, str]],
        log: Path,
        previous: dict[str, Any] | None,
        heard: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Builds the Brief from the failed jobs with their log, the diff of the Integration branch against `base`
        and, from the second Round on, the earlier Round's handoff and Gate verdicts.

        A Stage with `inputs` reads those, and the failed jobs only when it works on red CI.
        """
        inputs: list[dict[str, Any]] = []
        if failed or not stage.inputs:
            inputs.append({"source": "ci_log", "commit": commit, "jobs": failed, "path": str(log)})
        if any(item.source == "spec" for item in stage.inputs):
            inputs.append({"source": "spec", "path": str(self.context.run_dir.spec)})
        inputs += [{"source": source, "comments": said} for source, said in heard.items()]
        inputs.append({"source": "diff", "base": self.context.base_commit, "head": tip})
        if previous is not None and previous.get("output"):
            inputs.append(
                {"source": "stage", "node": stage.name, "round": previous["round"], "path": previous["output"]}
            )
        if previous is not None:
            inputs.append({"source": "cause", "kind": previous.get("cause", ""), "reason": previous.get("reason", "")})
            inputs += [
                {"source": "gate", "round": previous["round"], "path": path} for path in previous.get("verdicts", [])
            ]
        return {
            "run": self.context.run_id,
            "stage": stage.name,
            "round": number,
            "attempt": attempt,
            "base": self.context.base_commit,
            "base_ref": self.project.base,
            "integration_branch": self.context.integration,
            "head": tip,
            "inputs": inputs,
            "output_dir": str(attempt_dir),
        }

    def _discard(self, workspace: Path, branch: str, head: str, attempt_dir: Path) -> dict[str, Any]:
        """Resets the Workspace to `head` and keeps what the Attempt left in `discarded.patch`."""
        patch, files = self.git.discard(workspace, branch, head)
        if not patch:
            return {}
        return {"discarded": str(write_once(attempt_dir / "discarded.patch", patch)), "files": files}

    def _edit_pull_request(self, number: int, round_dir: Path) -> None:
        """Adds a line per Round to the body of the final PR: what failed and what the fix changed."""
        lines = []
        for fix in self.context.memory.ci_fixes:
            jobs = ", ".join(job["name"] for job in fix["jobs"])
            what = self.context.closing.defuse(fix.get("failure") or jobs or "The comments on the PR")
            if fix["outcome"] == "pushed":
                changed = self.context.closing.defuse(fix.get("change") or fix["summary"])
                lines.append(f"- Round {fix['round']}: {what}. Fixed by: {changed} ({fix['pushed'][:7]})\n")
            else:
                lines.append(f"- Round {fix['round']}: {what}. Not pushed: {one_line(fix['reason'])}\n")
        body = self.context.run_dir.pull_request.read_text().rstrip()
        heading = "Fixes" if any("comments" in fix for fix in self.context.memory.ci_fixes) else "CI fixes"
        path = write_once(round_dir / "pull_request.md", f"{body}\n\n{heading}:\n\n{''.join(lines)}")
        try:
            self.pull_requests.edit(self.context.memory.pull_request, path)
        except TrackerError as error:
            self.append(Event.PullRequestEditFailed, {"round": number, "reason": str(error)})
            return
        self.append(Event.PullRequestEdited, {"round": number, "path": str(path)})


# What the events of the CI fixer tell the readers of the Journal.

applies(Event.PullRequestEdited, Event.PullRequestEditFailed)(nothing)


@applies(Event.CiFixStarted)
def _apply_ci_fix_started(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.ci_fixes.append({**payload, "outcome": ""})


@applies(Event.CiFixCommitted)
def _apply_ci_fix_committed(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.ci_fixes[-1] = {**memory.ci_fixes[-1], **payload}


@applies(Event.CiFixGateFinished)
def _apply_ci_fix_gate_finished(memory: RunMemory, payload: dict[str, Any]) -> None:
    fix = memory.ci_fixes[-1]
    fix["verdicts"] = [*fix.get("verdicts", []), payload["verdict"]]


@applies(Event.CiFixFinished)
def _apply_ci_fix_finished(memory: RunMemory, payload: dict[str, Any]) -> None:
    """Ends the Round, where one that pushed its fix moves the wait on to the CI of the new head."""
    memory.ci_fixes[-1] = {**memory.ci_fixes[-1], **payload}
    if payload["outcome"] == "pushed" and memory.ci is not None:
        memory.ci = replace(memory.ci, commit=payload["pushed"], outcome="", jobs=(), artifact="")


@times(Event.CiFixStarted)
def _time_ci_fix_started(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    memory.enter(CI_FIXER.slot, "-", CI_FIXER.stage)


@times(Event.CiFixFinished)
def _time_ci_fix_finished(memory: TimelineMemory, payload: dict[str, Any]) -> None:
    if (fix := memory.entries.get(CI_FIXER.slot)) is None:
        return
    fix.outcome = payload["outcome"]
    fix.details = [
        f"round {payload['round']}",
        *([f"attempt {fix.attempt}"] if fix.attempt is not None else []),
        *([payload["cause"]] if "cause" in payload else []),
    ]
    memory.close(CI_FIXER.slot)
