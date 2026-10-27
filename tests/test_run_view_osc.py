import base64
import re

from factory.run_view import PROBE
from harness import FakeTty, Harness, run_id_of
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


def test_a_terminal_that_answers_the_probe_gets_the_tree_as_osc_7501_records(factory: Harness) -> None:
    factory.executor.scripts["implement"] = [HELLO]
    tty = factory.stdin = FakeTty()
    tty.type(REPLY)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    tty.close()
    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert result.out.index(PROBE) < result.out.index("\x1b]7501;state="), "records come only after the probe"
    root = [r for r in records(result.out) if r.get("app") == "factory"]
    assert root[-1]["state"] == "done"
    assert root[-1]["title"] == f"Run {run_id}"
    children = [r for r in records(result.out) if r.get("id") == "t1"]
    assert any(r["state"] == "working" and "implement" in r["msg"] and "round 1" in r["msg"] for r in children)
    assert children[-1]["state"] == "done"
    assert "t1" in re.sub(RECORD, "", result.out), "the text tree is printed as well"


def test_a_human_decision_blocks_the_ticket_and_the_root_with_a_question(factory: Harness) -> None:
    asking(factory)
    factory.executor.scripts["implement"] = [HELLO]
    tty = factory.stdin = FakeTty()
    tty.type(REPLY)
    tty.type_later("accept\n", 3)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    tty.close()
    assert result.code == 0, result.out
    found = records(result.out)
    blocked = [r for r in found if r["state"] == "blocked"]
    assert {r.get("id", "root") for r in blocked} == {"t1", "root"}
    assert all(r["kind"] == "question" for r in blocked)
    assert "\a" not in result.out, "Orca shows the question, so no BEL rings"
    assert found[-1]["state"] == "done"


def test_a_terminal_that_does_not_answer_the_probe_gets_only_the_text_tree(factory: Harness) -> None:
    factory.executor.scripts["implement"] = [HELLO]
    tty = factory.stdin = FakeTty()
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    tty.close()
    assert result.code == 0, result.out
    assert records(result.out) == []
    assert "t1" in result.out


def test_a_run_without_a_tty_sends_no_probe(factory: Harness) -> None:
    factory.executor.scripts["implement"] = [HELLO]
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert "\x1b]7501" not in result.out
