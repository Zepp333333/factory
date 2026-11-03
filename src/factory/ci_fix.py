"""The CI fixer works one Round on a red CI: a Fresh agent fixes the Integration branch, and the Gates must pass.

A Round runs in a Workspace and a Sandbox of its own on a branch at the Integration branch's tip, like the Describer.
After the fixer the host commits its change (ADR 0009) and judges the diff with the CI guard and the Gates named
in the Pipeline's `ci_fix`.
A Round that passes moves the Integration branch to that commit and pushes it, and a Round that fails leaves the
branch alone and gives the next Round its verdict.
"""

import re
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from factory import ci_guard, gates
from factory.artifacts import ProbeDir, write_json_once, write_once
from factory.attempt import Attempts
from factory.commands import RUN_LEVEL
from factory.config import Project
from factory.events import Event
from factory.executor import AttemptResult
from factory.git import GitError, HostGit
from factory.inputs import schema_errors
from factory.isolation import CleanupEvents
from factory.journal import CI_FIX_SLOT
from factory.landing import landing_remote
from factory.pipeline import CiFix, Gate, Pipeline
from factory.question import render_failed_logs
from factory.run_context import Ports, RunContext
from factory.sandbox import Mount, SandboxError
from factory.tracker import FailedJob, PullRequests, TrackerError
from factory.workbench import WorkbenchError

CI_FIX_NODE = "ci_fix"


def ci_fix_names(home: Path, run_id: str) -> tuple[str, str, Path]:
    """Returns the Sandbox, branch and Workspace of the CI fixer, none of which a Ticket's can equal."""
    return (
        f"cifix-{run_id}",
        f"factory/cifix-{run_id}",
        home / "workspaces" / f"cifix-{run_id}" / "workspace",
    )


def one_line(text: object) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


class CiFixer:
    """CiFixer works the Rounds of the CI fix of a Run.

    `append` journals an event and applies it to the Run state, and `beat` keeps the Run's heartbeat while it waits.
    """

    def __init__(
        self,
        append: Callable[[Event, dict[str, Any]], None],
        project: Project,
        pipeline: Pipeline,
        ports: Ports,
        home: Path,
        git: HostGit,
        attempts: Attempts,
        pull_requests: PullRequests,
        beat: Callable[[], None],
        tick: float,
        context: RunContext,
    ) -> None:
        self.append = append
        self.project = project
        self.pipeline = pipeline
        self.ports = ports
        self.home = home
        self.git = git
        self.attempts = attempts
        self.pull_requests = pull_requests
        self.beat = beat
        self.tick = tick
        self.context = context

    @property
    def rounds_left(self) -> bool:
        """Tells whether the Pipeline has a CI fix with a Round the Run has not taken yet."""
        ci_fix = self.pipeline.ci_fix
        return ci_fix is not None and len(self.context.memory.ci_fixes) < ci_fix.rounds

    def fix(self, commit: str, jobs: list[FailedJob]) -> str | None:
        """Works one Round on the red CI of `commit`, and returns the commit it pushed, or None if the Round failed.

        The Round's report goes to the final PR's body whatever its outcome.
        """
        ci_fix = self.pipeline.ci_fix
        assert ci_fix is not None
        run_dir = self.context.run_dir
        number = len(self.context.memory.ci_fixes) + 1
        previous = self.context.memory.ci_fixes[-1] if self.context.memory.ci_fixes else None
        round_dir = run_dir.ci_fix(number)
        log = write_once(round_dir / "failed.md", render_failed_logs(jobs))
        tip = self.git.resolve(self.context.integration)
        failed = [{"name": job.name, "url": job.url} for job in jobs]
        self.append(
            Event.CiFixStarted,
            {"round": number, "commit": commit, "jobs": failed, "artifact": str(log), "head": tip},
        )
        names = ci_fix_names(self.home, self.context.run_id)
        report = self._work(ci_fix, number, round_dir, names, tip, commit, failed, log, previous)
        self.append(Event.CiFixFinished, {"round": number, **report})
        self._edit_pull_request(number, round_dir)
        return report.get("pushed")

    def _work(
        self,
        ci_fix: CiFix,
        number: int,
        round_dir: Path,
        names: tuple[str, str, Path],
        tip: str,
        commit: str,
        failed: list[dict[str, str]],
        log: Path,
        previous: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Runs the Round's Attempts, guard and Gates in its Workspace, and returns its report.

        A Sandbox or git step that fails ends the Round as `sandbox_start`.
        The Workspace, the Sandbox and the branch are removed after it.
        """
        sandbox, branch, workspace = names
        try:
            self.git.create_branch(branch, tip)
            self.ports.workbench.create_workspace(self.project.repo, workspace, branch)
            self.ports.sandbox.create(sandbox, workspace, self._mounts())
            report = self._attempts(ci_fix, number, round_dir, names, tip, commit, failed, log, previous)
        except (SandboxError, WorkbenchError, GitError, OSError) as error:
            report = {"outcome": "failed", "cause": "sandbox_start", "reason": str(error)}
        self._clean_up(names, round_dir)
        return report

    def _attempts(
        self,
        ci_fix: CiFix,
        number: int,
        round_dir: Path,
        names: tuple[str, str, Path],
        tip: str,
        commit: str,
        failed: list[dict[str, str]],
        log: Path,
        previous: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Starts Attempts of the fixer until one gives output, then judges what it changed."""
        sandbox, branch, workspace = names
        work = ci_fix.agent
        report: dict[str, Any] = {"outcome": "failed", "cause": "no_attempt", "reason": "the fixer ran no Attempt"}
        for attempt in range(1, work.settings.attempts + 1):
            attempt_dir = round_dir / f"attempt-{attempt}"
            attempt_dir.mkdir(exist_ok=True)
            brief = self._brief(number, attempt, attempt_dir, tip, commit, failed, log, previous)
            started = {
                "ticket": CI_FIX_SLOT,
                "node": CI_FIX_NODE,
                "round": number,
                "attempt": attempt,
                **self.attempts.variant.of(work, work.settings),
                "head_before": tip,
                "stream": str(attempt_dir / "stream.ndjson"),
            }
            begun = self.ports.clock.now()
            try:
                result = self.attempts.work_apart(
                    work,
                    sandbox,
                    workspace,
                    attempt_dir,
                    brief,
                    CI_FIX_SLOT,
                    Event.AttemptStarted,
                    started,
                )
            except (SandboxError, WorkbenchError, GitError, OSError) as error:
                result = AttemptResult(output=None, failure="sandbox_start", reason=str(error))
            spent = {
                "duration": (self.ports.clock.now() - begun).total_seconds(),
                "usage": asdict(result.usage) if result.usage else None,
                "stats": asdict(result.stats) if result.stats else None,
            }
            key = {"ticket": CI_FIX_SLOT, "node": CI_FIX_NODE, "round": number, "attempt": attempt}
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
                "Factory-Stage": CI_FIX_NODE,
                "Factory-Attempt": str(attempt),
            }
            fixed = self.git.commit_all(workspace, branch, summary or f"{CI_FIX_NODE}: {self.context.run_id}", trailers)
            record = {
                "outcome": "done",
                "output": str(output),
                "head_before": tip,
                "head_after": fixed or tip,
                **spent,
            }
            write_json_once(attempt_dir / "attempt.json", record)
            self.append(Event.AttemptFinished, {**key, **record})
            said = {
                "output": str(output),
                **{field: one_line(result.output[field]) for field in ("failure", "change", "summary", "handoff")},
            }
            return {**said, **self._check(ci_fix, number, round_dir, names, tip, fixed)}
        return report

    def _check(
        self, ci_fix: CiFix, number: int, round_dir: Path, names: tuple[str, str, Path], tip: str, fixed: str | None
    ) -> dict[str, Any]:
        """Judges the fixer's commit with the CI guard and the Gates, and lands it on the Integration branch.

        A Gate by diff, not the agent's word, refuses a change of the CI config or a deleted, skipped or xfailed test.
        The first check that fails ends the Round with its verdict, and the Integration branch stays where it was.
        """
        sandbox, branch, workspace = names
        if fixed is None:
            return {"outcome": "failed", "cause": "no_change", "reason": "the fixer changed nothing", "verdicts": []}
        found = ci_guard.problems(self.git.changed_files(tip, fixed), self.git.diff(tip, fixed))
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
        verdicts = [str(guard)]
        if found:
            write_once(round_dir / "fix.patch", self.git.diff(tip, fixed) + "\n")
            return {"outcome": "failed", "cause": "guard", "reason": "; ".join(found), "verdicts": verdicts}
        results: dict[str, gates.ProbeResult] = {}
        for name in ci_fix.gates:
            gate = self.pipeline.nodes[name]
            assert isinstance(gate, Gate)
            try:
                outcome, reasons, verdict = self._gate(gate, number, round_dir, names, tip, results)
            except SandboxError as error:
                return {"outcome": "failed", "cause": "sandbox_lost", "reason": str(error), "verdicts": verdicts}
            verdicts.append(str(verdict))
            self.append(
                Event.CiFixGateFinished,
                {
                    "round": number,
                    "node": name,
                    "probe": 1,
                    **({"from": gate.from_} if gate.from_ is not None else {}),
                    "outcome": outcome,
                    "reasons": reasons,
                    "verdict": str(verdict),
                },
            )
            if outcome != "pass":
                write_once(round_dir / "fix.patch", self.git.diff(tip, fixed) + "\n")
                reason = f"Gate {name} failed: {'; '.join(reasons)}" if reasons else f"Gate {name} failed"
                return {"outcome": "failed", "cause": "gate", "reason": reason, "verdicts": verdicts}
        try:
            self.git.fast_forward(self.context.integration, fixed, tip)
            remote = landing_remote(self.git, self.pull_requests)
            self.git.push(remote, self.context.integration)
        except GitError as error:
            return {"outcome": "failed", "cause": "push", "reason": str(error), "verdicts": verdicts}
        self.append(
            Event.IntegrationBranchPushed, {"remote": remote, "branch": self.context.integration, "commit": fixed}
        )
        return {"outcome": "pushed", "pushed": fixed, "verdicts": verdicts}

    def _gate(
        self,
        gate: Gate,
        number: int,
        round_dir: Path,
        names: tuple[str, str, Path],
        tip: str,
        results: dict[str, gates.ProbeResult],
    ) -> tuple[str, list[str], Path]:
        """Runs the Gate's one Probe in the Round's Sandbox on the fixer's commit and returns the verdict.

        A Gate with `from` runs no Probe, reads the finished Probe of its source Gate, which came before it, and writes
        under `from-<source>-probe-1/`.
        """
        sandbox, branch, workspace = names
        probe_dir = ProbeDir(round_dir / gate.name / "probe-1")
        if gate.from_ is not None:
            probe = replace(results[gate.from_], exit_code=0, timed_out=False)
            probe_dir = ProbeDir(round_dir / gate.name / f"from-{gate.from_}-probe-1")
            probe_dir.root.mkdir(parents=True)
        else:
            probe = self._probe(gate, probe_dir, sandbox, workspace, branch)
            results[gate.name] = probe
        if gates.KINDS[gate.kind].evaluates_changed_lines:
            probe = replace(probe, changed=self.git.changed_lines(workspace, branch, tip))
        verdict = gates.evaluate(gate.name, gate.kind, gate.with_, probe)
        return verdict["outcome"], verdict["reasons"], write_json_once(probe_dir.verdict, verdict)

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
        number: int,
        attempt: int,
        attempt_dir: Path,
        tip: str,
        commit: str,
        failed: list[dict[str, str]],
        log: Path,
        previous: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Builds the Brief from the failed jobs with their log, the diff of the Integration branch against `base`
        and, from the second Round on, the earlier Round's handoff and Gate verdicts.
        """
        inputs: list[dict[str, Any]] = [
            {"source": "ci_log", "commit": commit, "jobs": failed, "path": str(log)},
            {"source": "diff", "base": self.context.base_commit, "head": tip},
        ]
        if previous is not None and previous.get("output"):
            inputs.append(
                {"source": "stage", "node": CI_FIX_NODE, "round": previous["round"], "path": previous["output"]}
            )
        if previous is not None:
            inputs.append({"source": "cause", "kind": previous.get("cause", ""), "reason": previous.get("reason", "")})
            inputs += [
                {"source": "gate", "round": previous["round"], "path": path} for path in previous.get("verdicts", [])
            ]
        return {
            "run": self.context.run_id,
            "stage": CI_FIX_NODE,
            "round": number,
            "attempt": attempt,
            "base": self.context.base_commit,
            "base_ref": self.project.base,
            "integration_branch": self.context.integration,
            "head": tip,
            "inputs": inputs,
            "output_dir": str(attempt_dir),
        }

    def _mounts(self) -> list[Mount]:
        """Lists what the fixer's Sandbox sees besides its Workspace: the Run's directory of CI fixes is writable."""
        run_dir = self.context.run_dir
        run_dir.agent_home.mkdir(exist_ok=True)
        return [
            Mount(self.git.common),
            Mount(run_dir.spec),
            Mount(run_dir.root / "tickets"),
            Mount(run_dir.root / "ci_fix", writable=True),
            Mount(run_dir.agent_home, writable=True),
            Mount(run_dir.kit),
            *(Mount(path) for path in self.ports.executor.reads()),
        ]

    def _discard(self, workspace: Path, branch: str, head: str, attempt_dir: Path) -> dict[str, Any]:
        """Resets the Workspace to `head` and keeps what the Attempt left in `discarded.patch`."""
        patch, files = self.git.discard(workspace, branch, head)
        if not patch:
            return {}
        return {"discarded": str(write_once(attempt_dir / "discarded.patch", patch)), "files": files}

    def _clean_up(self, names: tuple[str, str, Path], round_dir: Path) -> None:
        """Removes what the Round used, keeping what is left uncommitted in `discarded.patch`.

        Each step that ends is journaled, and a step that fails is journaled with its reason and leaves the steps
        after it undone.
        """
        sandbox, branch, workspace = names
        try:
            head = self.git.resolve(branch)
        except GitError:
            return

        def settle() -> None:
            patch, _ = self.git.discard(workspace, branch, head)
            if patch:
                write_once(round_dir / "discarded.patch", patch)

        def noop() -> None:
            pass

        events = CleanupEvents(
            removed=lambda: self.append(Event.CiFixSandboxRemoved, {"sandbox": sandbox}),
            left_behind=lambda reason: self.append(
                Event.CiFixSandboxLeftBehind, {"sandbox": sandbox, "reason": reason}
            ),
            workspace_removed=noop,
            branch_deleted=noop,
            failed=lambda step, error: self.append(Event.CiFixCleanupFailed, {"reason": str(error)}),
        )
        self.attempts.isolation.clean_up(
            sandbox, workspace if workspace.exists() else None, branch, head, events, settle
        )

    def _edit_pull_request(self, number: int, round_dir: Path) -> None:
        """Adds a line per Round to the body of the final PR: what failed and what the fix changed."""
        lines = []
        for fix in self.context.memory.ci_fixes:
            what = self.context.tracker.defuse(fix.get("failure") or ", ".join(job["name"] for job in fix["jobs"]))
            if fix["outcome"] == "pushed":
                changed = self.context.tracker.defuse(fix["change"])
                lines.append(f"- Round {fix['round']}: {what}. Fixed by: {changed} ({fix['pushed'][:7]})\n")
            else:
                lines.append(f"- Round {fix['round']}: {what}. Not pushed: {one_line(fix['reason'])}\n")
        body = self.context.run_dir.pull_request.read_text().rstrip()
        path = write_once(round_dir / "pull_request.md", f"{body}\n\nCI fixes:\n\n{''.join(lines)}")
        try:
            self.pull_requests.edit(self.context.memory.pull_request, path)
        except TrackerError as error:
            self.append(Event.PullRequestEditFailed, {"round": number, "reason": str(error)})
            return
        self.append(Event.PullRequestEdited, {"round": number, "path": str(path)})
