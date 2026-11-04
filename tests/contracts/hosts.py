"""The host CLIs of the registered kinds, faked on the PATH, so each kind's own `make` reaches its fake.

`gh` and `glab` are on the PATH of every Harness, `sbx` and `orca` run the harness's fakes on its state as well,
and `claude` runs the scripted agent on the scenario that `Host.play` writes.
A contract module runs every kind that `wiring` registers for its port, and with `harness` also every kind the harness
registers in its place, and it fails for a kind that has no entry in the module's table of fakes.
"""

import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from factory.ports.kind import Kind
from factory.wiring import REGISTRIES, Machine, Registries, Registry, made
from harness import AGENT, FAKE_ORCA, FAKE_SBX, AgentScript, Harness, wrap, write_scenario

# The page that tells how to add a kind and its fake to the contract of its port.
GUIDE = "docs/guide/07-adding-an-adapter.md"


@dataclass
class Host:
    """A Host is the machine the contract tests run the registered kinds on, with every host CLI faked.

    `scenario` is the file the fake `claude` plays.
    """

    factory: Harness
    scenario: Path

    @property
    def tools(self) -> Machine:
        """Returns a Machine with its own Tools, which reach the fakes through the PATH as the real CLIs would."""
        return Machine(home=self.factory.home)

    def play(self, script: AgentScript, stage: str) -> None:
        """Has the next agent of `stage` play `script`, the fake `claude` and the harness's ScriptedExecutor alike."""
        write_scenario(script, self.scenario)
        self.factory.executor.scripts[stage] = script


@pytest.fixture
def host(factory: Harness) -> Iterator[Host]:
    bin = factory.bin
    scenario = factory.root / "claude" / "scenario.json"
    scenario.parent.mkdir()
    wrap(bin, "sbx", sys.executable, str(FAKE_SBX), str(factory.sbx.root))
    wrap(bin, "orca", sys.executable, str(FAKE_ORCA), str(factory.orca.root))
    wrap(bin, "claude", sys.executable, str(AGENT), str(scenario))
    yield Host(factory, scenario)


@dataclass(frozen=True)
class Picked:
    """A Picked names one kind the contract runs: its `name` among the kinds of `source`, the registries of `wiring` or
    of the harness, or the fakes that only the `tests` know."""

    source: str
    name: str

    def __str__(self) -> str:
        return f"{self.source}-{self.name}"


def kinds(registry: Registry[Any], harness: bool = False) -> list[Picked]:
    """Lists the kinds of a registry of `wiring`, and with `harness` the same names once more as the harness's."""
    names = registry.names()
    return [Picked("wiring", name) for name in names] + [Picked("harness", name) for name in names if harness]


def fake_of[F](picked: Picked, port: str, faked: Mapping[str, F]) -> F:
    """Returns the entry of the picked kind in `faked`, the table of fakes of the port's contract module.

    It fails the test for a kind without one, so a kind that `wiring` registers never skips its port's contract.
    """
    if picked.name not in faked:
        pytest.fail(f"the {port} kind {picked.name!r} has no fake in the contract of its port, see {GUIDE}")
    return faked[picked.name]


def made_kind(picked: Picked, host: Host, port: str, faked: Mapping[str, object]) -> Any:
    """Makes the adapter of the picked kind with the settings of a block that names only the kind."""
    fake_of(picked, port, faked)
    registries: Registries = REGISTRIES if picked.source == "wiring" else host.factory.registries
    kind: Kind[Any] = getattr(registries, port)[picked.name]
    return made(kind, host.tools)
