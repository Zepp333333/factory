"""The kind `plan` is the Run Stage of the Planner, which selects the Tickets of the Run, in its order, from those the
Tracker read.

The Planner is a Fresh agent, and its Stage is of the group of the kinds that select the Tickets.
"""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from factory.core.events import Event, applies, measures
from factory.core.run_agent import PLANNER
from factory.core.states import SANDBOX_FAILURES
from factory.journal.artifacts import write_json_once
from factory.journal.metrics_memory import run_stage_ended, run_stage_started
from factory.journal.run_state import RunMemory
from factory.pipeline.pipeline import EMPTY, SELECTED, TRACKER_INPUTS, Role, RunStage
from factory.pipeline.run_kinds import NodeRunner, RunNodeKind, fixed
from factory.ports.executor import AttemptResult
from factory.run.inputs import schema_errors
from factory.run.node_tools import NodeTools
from factory.run.nodes.select import ORDER, take


class PlanRunner(NodeRunner):
    """PlanRunner has the Planner say which Tickets the Run takes, in its order, and takes them once.

    It ends in whether it took any, or in `failed` when the Planner gave no plan that stands.
    """

    def __init__(self, stage: RunStage, tools: NodeTools) -> None:
        self.stage = stage
        self.tools = tools
        self.context = tools.context
        self.append = tools.run_tools.append
        self.run_agents = tools.run_tools.run_agents

    def step(self) -> str:
        if not self.context.memory.selected:
            output = self.plan(self.stage)
            if output is None:
                return "failed"
            plan = json.loads(output.read_text())
            reasons = {entry["ticket"]: entry["reason"] for entry in plan["left_out"]}
            judged_on = {"role": self.stage.role.name, "output": str(output)}
            take(self.tools, self.stage.name, judged_on, plan["tickets"], reasons)
        return SELECTED if self.context.tickets else EMPTY

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
                PLANNER, work, attempt_dir, self._brief(stage, attempt, attempt_dir), PLANNER.start, started
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


KIND = RunNodeKind(
    name="plan",
    outcomes=fixed(SELECTED, EMPTY, "failed"),
    order=ORDER,
    agent=PLANNER,
    takes=TRACKER_INPUTS,
    narrows=True,
    make=PlanRunner,
)


# What the events of the Planner tell the readers of the Journal.


@applies(Event.PlannerFinished)
def _apply_planner_finished(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.planned = payload["output"]


@applies(Event.PlannerFailed)
def _apply_planner_failed(memory: RunMemory, payload: dict[str, Any]) -> None:
    memory.plan_failures.append(payload)


measures(Event.PlannerStarted)(run_stage_started)
measures(Event.PlannerFinished, Event.PlannerFailed)(run_stage_ended)
