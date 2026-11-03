"""The Ports are the Executors, Sandbox, Workbench, Clock and Agent kit of one Run, which most phases take."""

from collections.abc import Mapping
from dataclasses import dataclass

from factory.core.clock import Clock
from factory.ports.executor import AgentKit, Executor
from factory.ports.sandbox import Sandbox
from factory.ports.workbench import Workbench


@dataclass(frozen=True)
class Ports:
    """The Executors, Sandbox, Workbench and Clock one Run works through, and the Agent kit each Executor translates.

    `executors` holds every Executor the Pipeline names, by its kind name, and each node runs on the one its
    AgentSettings name.
    """

    executors: Mapping[str, Executor]
    sandbox: Sandbox
    workbench: Workbench
    clock: Clock
    kit: AgentKit
