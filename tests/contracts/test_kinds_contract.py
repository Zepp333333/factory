"""Every kind that `wiring` registers runs through the contract module of its port, on a fake of its host CLI."""

import pytest

import test_executor_contract
import test_pull_requests_contract
import test_sandbox_contract
import test_tracker_contract
import test_workbench_contract
from factory.wiring import REGISTRIES
from hosts import Picked, fake_of

# The table of fakes of each port's contract module, by the port's registry.
TABLES: dict[str, dict[str, object]] = {
    "executors": dict(test_executor_contract.FAKED),
    "sandboxes": dict(test_sandbox_contract.FAKED),
    "workbenches": dict(test_workbench_contract.FAKED),
    "trackers": dict(test_tracker_contract.FAKED),
    "pull_requests": dict(test_pull_requests_contract.FAKED),
}


@pytest.mark.parametrize("port", TABLES)
def test_the_fakes_of_a_ports_contract_are_the_kinds_wiring_registers_for_it(port: str) -> None:
    assert set(TABLES[port]) == set(getattr(REGISTRIES, port).names())


def test_a_kind_without_a_fake_fails_the_contract_of_its_port() -> None:
    registries = REGISTRIES.copy()
    registries.executors.register("codex", REGISTRIES.executors["claude-code"])

    with pytest.raises(pytest.fail.Exception, match="'codex' has no fake in the contract of its port"):
        for name in registries.executors.names():
            fake_of(Picked("wiring", name), "executors", TABLES["executors"])
