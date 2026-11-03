import base64
import re

import pytest

from factory.adapters.terminal_workbench import PROBE, OrcaWorkbench, TerminalWorkbench
from factory.core.states import RunState, TicketState
from factory.ports.kind import Kind
from factory.ports.workbench import RunTree
from harness import FakeTty, Harness, LingeringWorkbench, run_id_of
from test_human_decider import HELLO, TICKET, asking

REPLY = "\x1b]7501;?:states=idle,working,done,blocked,error:kinds=permission,question,auth\x1b\\"
RECORD = re.compile(r"\x1b\]7501;([^\x1b]*)\x1b\\")


def records(out: str) -> list[dict[str, str]]:
    """Returns the OSC 7501 records in `out`, with msg and title decoded."""
    found = []
    for body in RECORD.findall(out):
        if body == "?":
            continue
        fields = dict(part.split("=", 1) for part in body.split(":") if "=" in part)
        for key in ("msg", "title"):
            if key in fields:
                fields[key] = base64.b64decode(fields[key]).decode()
        found.append(fields)
    return found


def use_orca(factory: Harness) -> None:
    """Has the Run pick the real OrcaWorkbench, on the fake `orca` and the Machine's stdin."""
    factory.project["workbench"] = "orca"
    factory.registries.workbenches.register(
        "orca", Kind(lambda settings, tools: OrcaWorkbench(factory.orca.command, tools.owner_input))
    )


def use_terminal(factory: Harness) -> None:
    """Has the Run pick the real TerminalWorkbench."""
    factory.project["workbench"] = "terminal"
    factory.registries.workbenches.register("terminal", Kind(lambda settings, tools: TerminalWorkbench()))


def test_a_terminal_that_answers_the_probe_gets_the_tree_as_osc_7501_records_and_a_human_decision_blocks_it(
    factory: Harness,
) -> None:
    use_orca(factory)
    asking(factory)
    factory.executor.scripts["implement"] = [HELLO]
    tty = factory.stdin = FakeTty()
    tty.type(REPLY)
    tty.type_later("accept\n", 3)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    tty.close()
    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert result.out.index(PROBE) < result.out.index("\x1b]7501;state="), "records come only after the probe"
    found = records(result.out)
    root = [r for r in found if r.get("app") == "factory"]
    assert root[-1]["state"] == "done"
    assert root[-1]["title"] == f"Run {run_id}"
    children = [r for r in found if r.get("id") == "t1"]
    assert any(r["state"] == "working" and "implement" in r["msg"] and "round 1" in r["msg"] for r in children)
    assert children[-1]["state"] == "done"
    assert "t1" in re.sub(RECORD, "", result.out), "the text tree is printed as well"
    blocked = [r for r in found if r["state"] == "blocked"]
    assert {r.get("id", "root") for r in blocked} == {"t1", "root"}
    assert all(r["kind"] == "question" for r in blocked)
    assert "\a" not in result.out, "Orca shows the question, so no BEL rings"
    assert found[-1]["state"] == "done"


def test_a_terminal_that_does_not_answer_the_probe_gets_only_the_text_tree_and_a_bel(factory: Harness) -> None:
    use_orca(factory)
    asking(factory)
    factory.executor.scripts["implement"] = [HELLO]
    tty = factory.stdin = FakeTty()
    tty.type_later("accept\n", 3)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    tty.close()
    assert result.code == 0, result.out
    assert PROBE in result.out
    assert records(result.out) == []
    assert "t1" in result.out
    assert result.out.count("\a") == 1


@pytest.mark.parametrize("reply", [REPLY, ""], ids=["answered", "unanswered"])
def test_what_the_owner_typed_while_orca_read_the_probe_still_answers_the_decision(
    factory: Harness, reply: str
) -> None:
    use_orca(factory)
    asking(factory)
    factory.executor.scripts["implement"] = [HELLO]
    tty = factory.stdin = FakeTty()
    tty.type(f"{reply}accept\n")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    tty.close()
    assert result.code == 0, result.out
    assert factory.events(run_id_of(result.out), "DecisionDecided")[0]["outcome"] == "accept"


def test_a_plain_terminal_gets_a_bel_for_a_waiting_decision_and_no_probe_and_no_record(factory: Harness) -> None:
    use_terminal(factory)
    asking(factory)
    factory.executor.scripts["implement"] = [HELLO]
    tty = factory.stdin = FakeTty()
    tty.type_later("accept\n", 3)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    tty.close()
    assert result.code == 0, result.out
    assert result.out.count("\a") == 1
    assert "\x1b]" not in result.out, "a plain terminal gets no OSC at all"


class WatchingWorkbench(LingeringWorkbench):
    """WatchingWorkbench is a Workbench of another vendor, which keeps what the Run shows it."""

    def __init__(self) -> None:
        super().__init__()
        self.shown: list[RunTree] = []
        self.needs: list[bool] = []

    def show_run(self, tree: RunTree) -> None:
        self.shown.append(tree)

    def needs_owner(self, on: bool) -> None:
        self.needs.append(on)


def test_a_workbench_gets_the_run_tree_and_is_told_when_the_owner_is_needed_and_no_longer_is(factory: Harness) -> None:
    workbench = factory.workbench_override = WatchingWorkbench()
    asking(factory)
    factory.executor.scripts["implement"] = [HELLO]
    tty = factory.stdin = FakeTty()
    tty.type_later("accept\n", 3)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    tty.close()
    assert result.code == 0, result.out
    assert workbench.needs == [True, False]
    assert "\a" not in result.out, "the Workbench alone tells the owner"
    run_id = run_id_of(result.out)
    assert {tree.id for tree in workbench.shown} == {run_id}
    assert workbench.shown[0].state == RunState.RUNNING
    working = [t for tree in workbench.shown for t in tree.tickets if t.state == TicketState.ACTIVE]
    assert any(t.id == "t1" and t.node == "implement" and t.round == 1 for t in working)
    assert any(tree.waiting == ("t1",) for tree in workbench.shown)
    last = workbench.shown[-1]
    assert last.state == RunState.DONE
    assert last.waiting == ()
