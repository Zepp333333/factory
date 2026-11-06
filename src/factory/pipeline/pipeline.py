"""Pipeline is the checked model of a Pipeline's nodes and routes that the Orchestrator walks."""

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ClassVar, Literal

from factory.core.run_agent import DESCRIBER, RunAgent

# `project` loads the Target project's own agent config from the Workspace, `hermetic` loads none of it.
Profile = Literal["project", "hermetic"]

STAGE_OUTCOMES = ("done", "failed")
LIMITED_STAGE_OUTCOMES = ("done", "exhausted", "failed")
GATE_OUTCOMES = ("pass", "fail")

# The Operations that act on the Tracker through its TicketBoard, in a Pipeline and in a Run graph alike.
ASSIGN = "assign"
LABEL = "label"
COMMENT = "comment"
TRACKER_OPERATIONS = (ASSIGN, LABEL, COMMENT)

OPERATION_OUTCOMES = {
    "merge": ("merged",),
    "catch_up": ("clean", "conflict"),
    "file_issues": ("filed", "refused", "failed"),
    **dict.fromkeys(TRACKER_OPERATIONS, ("done",)),
}

# These outcomes of an Operation leave an artifact, such as the conflict that `catch_up` found.
# The `refused` of `file_issues` is one of them, and it escalates when the Pipeline does not route it.
# The `failed` of `file_issues` leaves one too, but it always escalates, so the graph checks treat it as an escalating
# outcome.
OPERATION_ARTIFACT_OUTCOMES = ("conflict", "refused")

# Unmapped escalating outcomes go to `escalate_to`, and only `refused` of them leaves an artifact.
ESCALATING_OUTCOMES = ("exhausted", "failed", "escalate", "refused")

BUILTIN_INPUTS = ("spec", "ticket", "cause")

# What a Tracker Operation does when the Tracker fails it: `warn` journals, shows and goes on, `retry` tries again after
# a wait, and `escalate` ends the node `failed`.
WARN = "warn"
RETRY = "retry"
ESCALATE = "escalate"
ON_FAIL = (WARN, RETRY, ESCALATE)

# The target of a Run node that acts on the Spec, and not on each Ticket of the Run.
SPEC_TARGET = "spec"

# What the text of a `comment` may name as `${name}`, of which only a comment on a Ticket has `ticket`.
COMMENT_REFERENCES = ("run", "branch", "spec", "ticket")

# The Run node kinds: the Ticket Pipeline over the Tickets of the Run, the PR, and the wait for the CI of its PR.
TICKETS = "tickets"
OPEN_PR = "open_pr"
WAIT_CI = "wait_ci"


@dataclass(frozen=True)
class Read:
    """Read is an output field a Role expects from the Stages it reads, with the JSON type it expects."""

    type: str
    optional: bool


@dataclass(frozen=True)
class BodyRules:
    """BodyRules name the headings and words that the PR body of a Describer Role must hold.

    A heading stands on a Markdown heading line of its own, and a word may stand anywhere in the body.
    """

    headings: tuple[str, ...] = ()
    words: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.headings or self.words)


@dataclass(frozen=True)
class Role:
    name: str
    prompt: Path
    output: Path
    skills: tuple[str, ...] = ()
    reads: dict[str, Read] = field(default_factory=dict, compare=False)
    body: BodyRules = BodyRules()

    # The final schema after all Layers, or None for a stand-in of a Role that failed to load.
    schema: dict[str, Any] | None = field(default=None, compare=False)


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
    effort: str | None = None


@dataclass(frozen=True)
class AgentSettings:
    """AgentSettings is what a Fresh agent runs with, from the Pipeline file through the Executor into the Journal.

    `executor` names the Executor the agent runs on: the project's, unless the block names another.
    `effort` is None for the Executor's own default.
    `escalate` is a Stage's, and `for_round` applies it.
    """

    model: str
    executor: str
    profile: Profile = "project"
    attempts: int = 1
    timeout: float = 3600.0
    stall: float | None = None
    effort: str | None = None
    escalate: Escalate | None = None

    def for_round(self, counted: int) -> "AgentSettings":
        """Returns the settings of the Round number `counted` since the node's count last started over.

        An escalation that names no effort keeps the node's own.
        """
        escalate = self.escalate
        if escalate is None or counted < escalate.from_round:
            return self
        return replace(self, model=escalate.model, effort=self.effort if escalate.effort is None else escalate.effort)


@dataclass(frozen=True)
class AgentWork:
    """AgentWork is what an Attempt of a Fresh agent runs with, for a Stage, a model Decider or a RunAgent.

    `commits` says whether the Attempt's changes get a Stage boundary commit, and `outcomes` is the closed list that
    a model Decider's output must name, which a Stage has none of.
    """

    name: str
    role: Role
    settings: AgentSettings
    commits: bool
    inputs: tuple[Input, ...] = ()
    outcomes: tuple[str, ...] | None = None


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
    settings: AgentSettings
    inputs: tuple[Input, ...]
    next: dict[str, str]
    commits: bool
    max_rounds: int | None = None
    resets: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def outcomes(self) -> tuple[str, ...]:
        return STAGE_OUTCOMES if self.max_rounds is None else LIMITED_STAGE_OUTCOMES

    @property
    def agent(self) -> AgentWork:
        return AgentWork(
            name=self.name, role=self.role, settings=self.settings, commits=self.commits, inputs=self.inputs
        )


@dataclass(frozen=True)
class Gate:
    """A Gate holds its `with` resolved against its kind, with defaults filled in.

    A Gate with `from_` runs no Probe and reads the Probe of that other Gate.
    A Gate with `reuse` ends `pass` without a Probe when the Ticket already passed the same kind and `with` on the
    same git tree, and `reuse: false` is for a command that depends on more than the tree.
    """

    KIND: ClassVar[str] = "gate"
    HOLDS_WINDOW: ClassVar[bool] = True
    name: str
    kind: str
    with_: dict[str, Any]
    clean: bool
    next: dict[str, str]
    inputs: tuple[Input, ...] = ()
    resets: dict[str, tuple[str, ...]] = field(default_factory=dict)
    from_: str | None = None
    reuse: bool = True

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
    settings: AgentSettings
    function: str | None = None
    with_: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    """A Decision lists its declared outcomes, and `outcomes` adds the implicit `escalate`.

    `descriptions` say what each declared outcome means, for the question card a human Decider answers.
    """

    KIND: ClassVar[str] = "decision"
    HOLDS_WINDOW: ClassVar[bool] = False
    name: str
    decider: Decider
    declared: tuple[str, ...]
    inputs: tuple[Input, ...]
    next: dict[str, str]
    resets: dict[str, tuple[str, ...]] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)

    @property
    def outcomes(self) -> tuple[str, ...]:
        return (*self.declared, "escalate")

    @property
    def agent(self) -> AgentWork:
        """Returns what a model Decider's Attempts run with: a Stage's settings, but read-only."""
        decider = self.decider
        assert decider.role is not None, "Config proves that a model Decider has a Role"
        return AgentWork(
            name=self.name,
            role=decider.role,
            settings=decider.settings,
            commits=False,
            inputs=self.inputs,
            outcomes=self.outcomes,
        )


@dataclass(frozen=True)
class TrackerAction:
    """A TrackerAction is what a Tracker Operation does on each of its targets, in the order of `actions`.

    `add` and `remove` are the labels of a `label`, and `text` the static text of a `comment`.
    `on_spec` tells a Run node to act on the Spec, in place of each Ticket of the Run.
    """

    kind: str
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    text: str = ""
    on_spec: bool = False
    on_fail: str = WARN

    @property
    def actions(self) -> tuple[str, ...]:
        """Returns the name of each action, by which the Journal tells which of them a target has had."""
        if self.kind != LABEL:
            return (self.kind,)
        return (*(f"add:{label}" for label in self.add), *(f"remove:{label}" for label in self.remove))

    @property
    def outcomes(self) -> tuple[str, ...]:
        """Returns the outcomes it can end in, where only `escalate` ever ends `failed`."""
        return ("done", "failed") if self.on_fail == ESCALATE else ("done",)


@dataclass(frozen=True)
class Operation:
    """An Operation has the `with` of its kind, which `file_issues` reads: the Stages whose issues it files in
    `from_`, and the `labels` and `milestone` of every issue it files, and a Tracker Operation its `tracker`.
    """

    KIND: ClassVar[str] = "operation"
    HOLDS_WINDOW: ClassVar[bool] = True
    name: str
    kind: str
    next: dict[str, str]
    inputs: tuple[Input, ...] = ()
    resets: dict[str, tuple[str, ...]] = field(default_factory=dict)
    from_: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    milestone: str | None = None
    tracker: TrackerAction | None = None

    @property
    def outcomes(self) -> tuple[str, ...]:
        return self.tracker.outcomes if self.tracker else OPERATION_OUTCOMES.get(self.kind, ())

    @property
    def takes_window(self) -> bool:
        """Tells whether the Operation changes branches under the Merge window, which only `catch_up` and `merge` do."""
        return self.kind != "file_issues" and self.kind not in TRACKER_OPERATIONS


@dataclass(frozen=True)
class RunNodeKind:
    """A RunNodeKind says what a Run node of that kind may do: its closed `outcomes`, the fields of its `with`, all of
    them required, and the kinds that `after` says must have run on every path to it.
    """

    outcomes: tuple[str, ...] = ()
    with_fields: tuple[str, ...] = ()
    after: tuple[str, ...] = ()


# The closed set of node kinds of a Run graph, as the kinds of a Gate or an Operation are of a Pipeline.
RUN_NODE_KINDS = {
    TICKETS: RunNodeKind(("all_merged", "some_merged", "none_merged"), with_fields=("pipeline",)),
    OPEN_PR: RunNodeKind(("opened", "failed"), after=(TICKETS,)),
    WAIT_CI: RunNodeKind(("green", "absent", "accepted")),
    **{kind: RunNodeKind(OPERATION_OUTCOMES[kind]) for kind in TRACKER_OPERATIONS},
}


@dataclass(frozen=True)
class RunNode:
    """A RunNode is a node of a Run graph, which the Orchestrator itself carries out once per Run.

    A node of a Tracker Operation holds what it does in `tracker`.
    """

    KIND: ClassVar[str] = "node"
    HOLDS_WINDOW: ClassVar[bool] = False
    name: str
    kind: str
    next: dict[str, str]
    with_: dict[str, Any] = field(default_factory=dict)
    inputs: tuple[Input, ...] = ()
    resets: dict[str, tuple[str, ...]] = field(default_factory=dict)
    tracker: TrackerAction | None = None

    @property
    def traits(self) -> RunNodeKind:
        """Returns what its kind says, or a bare RunNodeKind for an unknown kind, which is already an error."""
        return RUN_NODE_KINDS.get(self.kind, RunNodeKind())

    @property
    def outcomes(self) -> tuple[str, ...]:
        return self.tracker.outcomes if self.tracker else self.traits.outcomes


@dataclass(frozen=True)
class RunStage(Stage):
    """A RunStage is a Stage of a Run graph, which has no Ticket: the Describer, whose changes nobody commits."""

    KIND: ClassVar[str] = "node"
    kind: ClassVar[str] = "stage"


@dataclass(frozen=True)
class RunAgentSpec:
    """A RunAgentSpec is what the Pipeline sets of a RunAgent: its Role, its settings and what differs per agent.

    The Describer's Role says the headings and words of its `body`, and each of the CI fixer's `rounds` is a Fresh agent
    in a Workspace of its own, followed by the Gates named in `gates`.
    The Describer comes from the Run graph's Stage, and the CI fixer from the block `ci_fix` of the Pipeline file.
    """

    run_agent: RunAgent
    role: Role
    settings: AgentSettings
    rounds: int = 1
    gates: tuple[str, ...] = ()

    @property
    def agent(self) -> AgentWork:
        """Returns what its Attempts run with, whose changes the agent's own module commits or discards."""
        return AgentWork(name=self.run_agent.stage, role=self.role, settings=self.settings, commits=False)


Node = Stage | Gate | Decision | Operation | RunNode


@dataclass(frozen=True)
class Pipeline:
    """A Pipeline is one graph of the format of ADR 0004, and a Run graph is one whose nodes are RunNodes (ADR 0014)."""

    path: Path
    start: str
    nodes: dict[str, Node]
    escalate_to: str | None
    sandbox_retries: int = 2
    run_agents: dict[str, RunAgentSpec] = field(default_factory=dict)

    @property
    def describer(self) -> RunAgentSpec | None:
        """Returns the Describer, which writes the PR text, or None when the Run graph has no Stage for it."""
        return self.run_agents.get(DESCRIBER.name)

    @property
    def run_stage(self) -> RunStage | None:
        """Returns the Stage of a Run graph, or None for a Run graph without one and for a Ticket Pipeline."""
        return next((node for node in self.nodes.values() if isinstance(node, RunStage)), None)

    @property
    def executors(self) -> tuple[str, ...]:
        """Returns the names of the Executors the Pipeline's Fresh agents run on, in the order the Pipeline names them:
        its Stages, its model Deciders and its RunAgents."""
        found = [node.agent.settings.executor for node in self.nodes.values() if isinstance(node, Stage)]
        found += [
            node.decider.settings.executor
            for node in self.nodes.values()
            if isinstance(node, Decision) and node.decider.role is not None
        ]
        found += [spec.settings.executor for spec in self.run_agents.values()]
        return tuple(dict.fromkeys(found))

    @property
    def on_the_tracker(self) -> list[str]:
        """Returns the names of the nodes that act on the Tracker, in the order of the file."""
        return [name for name, node in self.nodes.items() if isinstance(node, Operation | RunNode) and node.tracker]

    def operations_of(self, kind: str) -> list[str]:
        """Returns the names of the Operations of `kind`, in the order of the Pipeline file."""
        return [name for name, node in self.nodes.items() if isinstance(node, Operation) and node.kind == kind]

    def run_nodes_of(self, kind: str) -> list[str]:
        """Returns the names of the Run nodes of `kind`, in the order of the Run graph file."""
        return [name for name, node in self.nodes.items() if isinstance(node, RunNode) and node.kind == kind]

    def route(self, node: Node, outcome: str) -> str:
        """Returns the node an outcome leads to, sending unmapped escalating outcomes to `escalate_to`."""
        target = node.next.get(outcome)
        if target is None and outcome in ESCALATING_OUTCOMES:
            target = self.escalate_to
        assert target is not None, f"{node.name} has no route for {outcome}"
        return target

    def sends_back_to_work(self, node: Node, outcome: str) -> bool:
        """Tells whether an outcome leads straight to a Stage, which makes a human Decider give reasons for it."""
        return isinstance(self.nodes.get(self.route(node, outcome)), Stage)
