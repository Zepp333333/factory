"""The Run Gates of a Run graph judge the commit of a Round of a Stage that commits, before the fix lands."""

from factory.pipeline.pipeline import GATE_OUTCOMES, Gate
from factory.pipeline.run_kinds import GATE, NodeRunner, RunNodeKind, fixed
from factory.run.node_tools import NodeTools


class GateRunner(NodeRunner):
    """GateRunner runs the Gate on the commit of the Round through the CI fixer."""

    def __init__(self, gate: Gate, tools: NodeTools) -> None:
        self.gate = gate
        self.fixer = tools.fixer

    def step(self) -> str:
        return self.fixer.run_gate(self.gate)


KIND = RunNodeKind(name=GATE, outcomes=fixed(*GATE_OUTCOMES), make=GateRunner)
