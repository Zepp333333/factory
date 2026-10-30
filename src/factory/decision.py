"""The Decision module owns what a Decision node does, from starting its Decider to the Artifact of its Ruling.

A Decider of kind `human` puts a question card to the owner and waits for the answer, one of kind `model` runs an
Attempt through the Attempt module, and one of kind `function` calls code of the factory.
The `deciders.KINDS` table says which a Decision does, so nothing here branches on a Decider kind's name.
The module also checks an answer or an override against the Decision it is meant for, and the same checks apply to
the Run's own Decision `@run`.
Per ADR 0001 a Decider has no side effects, so the Orchestrator carries a Ruling out: it restarts the Sandbox and
commits the owner's edits.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory import deciders, values
from factory.artifacts import write_json_once, write_once
from factory.attempt import Ended, Ports, resolve_input
from factory.commands import Limits
from factory.events import Event
from factory.git import HostGit
from factory.live import LiveTicket
from factory.names import sandbox_name
from factory.pipeline import Decision, Pipeline, Stage
from factory.pipeline_checks import merges_without_sync
from factory.question import CardOutcome, Link, render_card
from factory.run_context import RunContext
from factory.sandbox import SandboxError
from factory.throttle import parse_brake, parse_window


class Rejected(Exception):
    """A Rejected is the reason the Orchestrator turns a Command down."""


@dataclass(frozen=True)
class Override:
    """An Override is the owner's new outcome for the Decision of `round`, which `replaces` an outcome already made.

    A Decision still waiting has nothing to replace, and keeps the usual `path` of its outcome.
    """

    round: int
    path: Path | None = None
    replaces: str | None = None


@dataclass(frozen=True)
class Asking:
    """Asking is the Decision a Ticket or the Run waits at, which an answer must name.

    `subject` names who waits, `waiting` is the node and Round of the Decision, and `answered` tells that an answer
    was already taken from the subject.
    """

    subject: str
    waiting: tuple[str, int] | None
    answered: bool


def check_asked(asking: Asking, node: str | None, round: int | None) -> None:
    """Raises Rejected unless the answer names the Decision and Round the subject waits at now."""
    if node is None:
        if asking.answered:
            raise Rejected("already decided")
        raise Rejected(f"the answer names no Decision, as {asking.subject} waited for none when it was sent")
    if (node, round) != asking.waiting:
        raise Rejected("already decided")


def check_ruling(
    outcome: str, reasons: list[str], outcomes: tuple[str, ...], sends_back: Callable[[str], bool]
) -> deciders.Ruling:
    """Returns the Ruling of an answer or an override, or raises Rejected if its outcome is none of the Decision's
    or sends the Ticket back to work without reasons.
    """
    if (error := deciders.outcome_error(outcome, outcomes)) is not None:
        raise Rejected(error)
    if sends_back(outcome) and not reasons:
        raise Rejected(f"outcome {outcome} sends the Ticket back to work and needs reasons")
    return deciders.Ruling(outcome, reasons)


def check_limits(pipeline: Pipeline, limits: Limits) -> None:
    """Raises Rejected unless the Run can take the parallelism, round limits and brakes a `limits` Command sets."""
    max_parallel, rounds = limits.max_parallel, limits.rounds
    merges = merges_without_sync(pipeline.nodes, pipeline.start, pipeline.escalate_to)
    if max_parallel is not None and max_parallel > 1 and merges:
        source, merge = merges[0]
        raise Rejected(
            f"{max_parallel} Tickets at once need a sync before merge, but {source} reaches {merge} without one"
        )
    for window, shares in limits.throttle.items():
        try:
            parse_window(window)
            if shares is not None:
                parse_brake(shares)
        except values.Invalid as error:
            raise Rejected(f"{window}: {error}") from None
    for name in rounds:
        stage = pipeline.nodes.get(name)
        if not isinstance(stage, Stage) or stage.max_rounds is None:
            raise Rejected(f"{name} is no Stage with a round limit")


class Decisions:
    """Decisions starts the Decider of a Decision and records its Ruling.

    `append` journals an event and applies it to the Run state, and `ring` calls the owner when a question waits.
    """

    def __init__(
        self,
        append: Callable[[Event, dict[str, Any]], None],
        pipeline: Pipeline,
        ports: Ports,
        git: HostGit,
        context: RunContext,
        ring: Callable[[], None],
    ) -> None:
        self.append = append
        self.pipeline = pipeline
        self.ports = ports
        self.git = git
        self.context = context
        self.ring = ring

    def decide(
        self, ticket: LiveTicket, decision: Decision, step_attempt: Callable[[], Ended | bool]
    ) -> tuple[str, dict[str, Any]] | bool:
        """Steps the Ticket's Decider and returns the recorded outcome and Artifact, or whether the Ticket moved.

        A Decider that waits or runs an Attempt may not be done yet, and `step_attempt` starts or steps its Attempt.
        """
        kind = deciders.KINDS[decision.decider.kind]
        if kind.waits:
            return self._wait(ticket, decision)
        if kind.runs_attempt:
            ended = step_attempt()
            if not isinstance(ended, Ended):
                return ended
            ruling = self.model_ruling(ended)
        else:
            ruling = self._call_function(ticket, decision)
        return self.record(ticket, decision, ruling)

    def _wait(self, ticket: LiveTicket, decision: Decision) -> bool:
        """Puts the question to the owner when the Ticket enters a human Decision, and returns whether it did.

        The wait holds no slot and has no timeout, and the Ticket's Sandbox stays stopped until the answer comes.
        """
        if ticket.waiting is not None:
            return False
        card = self._write_card(ticket, decision)
        try:
            self.ports.sandbox.stop(sandbox_name(self.context.run_id, ticket.id))
        except SandboxError as error:
            self.append(Event.SandboxStopFailed, {"ticket": ticket.id, "reason": str(error)})
        assert ticket.workspace is not None
        self.append(
            Event.DecisionWaiting,
            {
                "ticket": ticket.id,
                "node": decision.name,
                "round": ticket.round,
                "card": str(card),
                "head": self.git.head(ticket.workspace, ticket.branch),
                "outcomes": list(decision.outcomes),
            },
        )
        self.ring()
        return True

    def _write_card(self, ticket: LiveTicket, decision: Decision) -> Path:
        """Renders the question card of the Decision's Round as an Artifact next to its outcome."""
        run_dir = self.context.run_dir
        title = next(t.title for t in self.context.snapshot.tickets if t.id == ticket.id)
        cause = None
        if ticket.cause is not None:
            cause = Link(f"{ticket.cause['kind']} {ticket.cause['node']}", Path(ticket.cause["path"]))
        verdicts = {}
        for artifact in ticket.artifacts:
            if artifact["kind"] == "gate":
                verdicts[artifact["node"]] = Link(f"gate {artifact['node']}", Path(artifact["path"]))
        inputs = [
            Link(entry["source"] if "node" not in entry else f"{entry['source']} {entry['node']}", Path(entry["path"]))
            for item in decision.inputs
            for entry in resolve_input(run_dir, ticket, item)
        ]
        outcomes = [
            CardOutcome(name, decision.descriptions.get(name, ""), self.pipeline.sends_back_to_work(decision, name))
            for name in decision.outcomes
        ]
        text = render_card(
            self.context.run_id, ticket.id, title, decision.name, cause, list(verdicts.values()), inputs, outcomes
        )
        return write_once(run_dir.decision(ticket.id, decision.name, ticket.round).with_name("question.md"), text)

    def _call_function(self, ticket: LiveTicket, decision: Decision) -> deciders.Ruling:
        """Runs the function of a function Decider on the outputs of the Stages the Decision reads."""
        assert decision.decider.function is not None
        outputs = []
        for item in decision.inputs:
            for entry in resolve_input(self.context.run_dir, ticket, item):
                if entry["source"] == "stage":
                    output = json.loads(Path(entry["path"]).read_text())
                    outputs.append(output if isinstance(output, dict) else {})
        return deciders.decide(decision.decider.function, decision.decider.with_, outputs)

    def model_ruling(self, ended: Ended) -> deciders.Ruling:
        """Returns what a model Decider decided, or `escalate` when every Attempt of its Round failed."""
        artifact = ended.artifact
        if ended.outcome != "done" or artifact is None:
            return deciders.Ruling("escalate", ["the model Decider failed all its Attempts"])
        output = json.loads(Path(artifact["path"]).read_text())
        return deciders.Ruling(output["outcome"], list(output.get("reasons", [])))

    def record(
        self,
        ticket: LiveTicket,
        decision: Decision,
        ruling: deciders.Ruling,
        waited: dict[str, str] | None = None,
        override: Override | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Writes the Decision's outcome with its reasons as an Artifact, and journals it.

        An `override` is the owner's, and is journaled as `DecisionOverridden`.

        The Orchestrator carries the outcome out by its route, and the artifact is the `cause` of the next node.
        The artifact of a human Decision also carries the `head_before..head_after` range of its waiting (ADR 0009),
        so the next Stage reads the owner's edits with `git diff` like an agent's.
        """
        round = override.round if override else ticket.round
        decider = deciders.OWNER if override else decision.decider.kind
        written = write_json_once(
            override.path
            if override and override.path
            else self.context.run_dir.decision(ticket.id, decision.name, round),
            {
                "decision": decision.name,
                "decider": decider,
                "outcome": ruling.outcome,
                "reasons": ruling.reasons,
                **({"overrides": override.replaces} if override and override.replaces else {}),
                **(waited or {}),
            },
        )
        key = {"ticket": ticket.id, "node": decision.name, "round": round}
        artifact = {"kind": "decision", "node": decision.name, "round": round, "path": str(written)}
        artifact.update(waited or {})
        self.append(
            Event.DecisionOverridden if override else Event.DecisionDecided,
            {
                **key,
                "decider": decider,
                "outcome": ruling.outcome,
                "reasons": ruling.reasons,
                "path": str(written),
                "artifact": artifact,
                **({} if override else {"duration": ticket.seconds_in_node(self.ports.clock.now())}),
            },
        )
        return ruling.outcome, artifact

    def restart_sandbox(self, ticket: LiveTicket) -> None:
        """Starts the Sandbox of a Ticket that waited for the owner, which the answer or the override ends."""
        try:
            self.ports.sandbox.start(sandbox_name(self.context.run_id, ticket.id))
        except SandboxError as error:
            self.append(Event.SandboxStartFailed, {"ticket": ticket.id, "reason": str(error)})

    def take_owner_edits(self, ticket: LiveTicket) -> dict[str, str]:
        """Commits what the owner left uncommitted in the Workspace while the Decision waited, as the human.

        Returns the `head_before` and `head_after` of the waiting, whose range holds the owner's commits.
        """
        assert ticket.workspace is not None
        trailers = {"Factory-Run": self.context.run_id, "Factory-Ticket": ticket.id, "Factory-Actor": "human"}
        self.git.commit_all(ticket.workspace, ticket.branch, f"Owner edits to {ticket.id}", trailers)
        waited = {"head_before": ticket.waiting_head, "head_after": self.git.head(ticket.workspace, ticket.branch)}
        if waited["head_after"] != waited["head_before"]:
            self.append(Event.OwnerEditsCommitted, {"ticket": ticket.id, **waited})
        return waited
