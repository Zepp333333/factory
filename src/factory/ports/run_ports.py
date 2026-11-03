"""The Ports are the Executor, Sandbox, Workbench, Clock and Agent kit one Run works through, which most phases take."""

from dataclasses import dataclass

from factory.core.clock import Clock
from factory.ports.executor import AgentKit, Executor
from factory.ports.sandbox import Sandbox
from factory.ports.workbench import Workbench


@dataclass(frozen=True)
class Ports:
    """The Executor, Sandbox, Workbench and Clock one Run works through, and the Agent kit its Executor translates."""

    executor: Executor
    sandbox: Sandbox
    workbench: Workbench
    clock: Clock
    kit: AgentKit
