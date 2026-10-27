"""Pipeline is the checked model of a Pipeline's nodes and routes that the Orchestrator walks."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from factory.executor import Profile

TERMINAL_NODES = ("done", "abandoned")
STAGE_OUTCOMES = ("done", "failed")
LIMITED_STAGE_OUTCOMES = ("done", "exhausted", "failed")
GATE_OUTCOMES = ("pass", "fail")
OPERATION_OUTCOMES = {"merge": ("merged",), "sync": ("clean", "conflict")}

# These outcomes of an Operation leave an artifact, such as the conflict that `sync` found.
OPERATION_ARTIFACT_OUTCOMES = ("conflict",)

# Unmapped escalating outcomes go to `escalate_to`, and none of them leaves an artifact.
ESCALATING_OUTCOMES = ("exhausted", "failed", "escalate")

BUILTIN_INPUTS = ("spec", "ticket", "cause")


@dataclass(frozen=True)
class Role:
    name: str
    prompt: Path
    output: Path
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class Input:
    """An Input is a builtin source such as `spec`, or a reference to the latest artifact of the named nodes."""

    source: str
    nodes: tuple[str, ...] = ()
    optional: bool = False
    all_rounds: bool = False

    def __str__(self) -> str:
        return " ".join([self.source, " or ".join(self.nodes)]).strip()


@dataclass(frozen=True)
class Escalate:
    from_round: int
    model: str


@dataclass(frozen=True)
class Stage:
    """A Stage may declare a limit on its Rounds per Ticket and a stronger model for its later Rounds.

    Both count the Rounds since `reset_rounds` last started the count over.
    Only a Stage with a round limit has the outcome `exhausted`.
    """

    KIND: ClassVar[str] = "stage"
    HOLDS_WINDOW: ClassVar[bool] = False
    name: str
    role: Role
    model: str
    inputs: tuple[Input, ...]
    next: dict[str, str]
    attempts: int
    timeout: float
    commits: bool
    profile: Profile = "project"
    max_rounds: int | None = None
    escalate: Escalate | None = None
    resets: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def outcomes(self) -> tuple[str, ...]:
        return STAGE_OUTCOMES if self.max_rounds is None else LIMITED_STAGE_OUTCOMES

    def model_for(self, counted: int) -> str:
        """Returns the model of the Stage's Round number `counted` since its count last started over."""
        if self.escalate is not None and counted >= self.escalate.from_round:
            return self.escalate.model
        return self.model


@dataclass(frozen=True)
class Gate:
    """A Gate holds its `with` resolved against its kind, with defaults filled in.

    A Gate with a `source` runs no Probe and reads the Probe of that other Gate."""

    KIND: ClassVar[str] = "gate"
    HOLDS_WINDOW: ClassVar[bool] = True
    name: str
    kind: str
    with_: dict[str, Any]
    clean: bool
    next: dict[str, str]
    inputs: tuple[Input, ...] = ()
    resets: dict[str, tuple[str, ...]] = field(default_factory=dict)
    source: str | None = None

    @property
    def outcomes(self) -> tuple[str, ...]:
        return GATE_OUTCOMES


@dataclass(frozen=True)
class Decider:
    """A Decider of kind `function` names a function of the factory and holds its resolved `with`.

    A Decider of kind `model` names a Role and runs as a Fresh agent, with the settings of a Stage that reads only.
    """

    kind: str
    role: Role | None
    model: str
    function: str | None = None
    with_: dict[str, Any] = field(default_factory=dict)
    profile: Profile = "project"
    attempts: int = 1
    timeout: float = 3600.0


@dataclass(frozen=True)
class Decision:
    KIND: ClassVar[str] = "decision"
    HOLDS_WINDOW: ClassVar[bool] = False
    name: str
    decider: Decider
    declared: tuple[str, ...]
    inputs: tuple[Input, ...]
    next: dict[str, str]
    resets: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def outcomes(self) -> tuple[str, ...]:
        return (*self.declared, "escalate")


@dataclass(frozen=True)
class Operation:
    KIND: ClassVar[str] = "operation"
    HOLDS_WINDOW: ClassVar[bool] = True
    name: str
    kind: str
    next: dict[str, str]
    inputs: tuple[Input, ...] = ()
    resets: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def outcomes(self) -> tuple[str, ...]:
        return OPERATION_OUTCOMES.get(self.kind, ())


@dataclass(frozen=True)
class Describer:
    """A Describer is the Fresh agent that writes the title and body of the Run's final PR after the last merge."""

    role: Role
    model: str
    profile: Profile
    attempts: int
    timeout: float


Node = Stage | Gate | Decision | Operation


@dataclass(frozen=True)
class Pipeline:
    path: Path
    start: str
    nodes: dict[str, Node]
    escalate_to: str | None
    sandbox_retries: int = 2
    describer: Describer | None = None

    def route(self, node: Node, outcome: str) -> str:
        """Returns the node an outcome leads to, sending unmapped escalating outcomes to `escalate_to`."""
        target = node.next.get(outcome)
        if target is None and outcome in ESCALATING_OUTCOMES:
            target = self.escalate_to
        assert target is not None, f"{node.name} has no route for {outcome}"
        return target
