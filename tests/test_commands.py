import json
import subprocess
from pathlib import Path

from harness import AgentScript, Harness, run_id_of, ticket_text
from test_deciders import RISK, deciding, review
from test_fix_loop import fix_loop
from test_human_decider import asking

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})
GIVE_UP = 20000


class Owner:
    """An Owner sends `factory` Commands to the Run from other processes, from the Run's sleep.

    Each step waits until its `ready` says so about the Run's id, and starts after the one before it ended.
    """

    def __init__(self, factory: Harness) -> None:
        self.factory = factory
        self.steps: list[tuple[object, list[str]]] = []
        self.procs: list[subprocess.Popen[str]] = []
        self.sleeps = 0
        factory.clock.on_sleep = self.on_sleep

    def then(self, ready: object, *argv: str) -> "Owner":
        self.steps.append((ready, list(argv)))
        return self

    def on_sleep(self, seconds: float) -> None:
        self.sleeps += 1
        assert self.sleeps < GIVE_UP, "the Run never got there"
        if not self.factory.run_ids() or len(self.procs) >= len(self.steps):
            return
        if self.procs and self.procs[-1].poll() is None:
            return
        run_id = self.factory.run_ids()[-1]
        ready, argv = self.steps[len(self.procs)]
        if ready(run_id):  # type: ignore[operator]
            self.procs.append(self.factory.spawn(argv[0], run_id, *argv[1:]))

    def results(self) -> list[tuple[int, str, str]]:
        out = []
        for proc in self.procs:
            stdout, stderr = proc.communicate(timeout=30)
            out.append((proc.returncode, stdout.strip(), stderr.strip()))
        return out


def started(factory: Harness, node: str, count: int = 1):  # type: ignore[no-untyped-def]
    return lambda run_id: len([e for e in factory.events(run_id, "AttemptStarted") if e["node"] == node]) >= count


def test_cancel_abandons_a_ticket_at_work_and_its_dependents_never_start(factory: Harness) -> None:
    factory.pipeline["defaults"]["sandbox_retries"] = 2
    factory.project["max_parallel"] = 2
    factory.executor.scripts["implement"] = HELLO
    factory.executor.ticket_scripts[("t1", "implement")] = AgentScript(
        files={"half.txt": "half\n"}, output=HELLO.output, wait_for=factory.root / "never"
    )
    owner = Owner(factory).then(started(factory, "implement"), "cancel", "t1")
    spec = factory.write_spec(
        "Greeting",
        "Say hello.",
        {
            "t1": ticket_text("Greet"),
            "t2": ticket_text("Greet again", ["t1"]),
            "t3": ticket_text("Wave"),
        },
    )

    result = factory.run(spec)

    assert result.code == 1, result.out + result.err
    run_id = run_id_of(result.out)
    assert owner.results() == [(0, "Ticket t1 cancelled, abandoned", "")]
    status = factory.cli("status", run_id).out.splitlines()
    assert status[0].startswith(f"Run {run_id}  partial  factory/{run_id}")
    assert [row.split()[:2] + [row.split()[6]] for row in status[2:5]] == [
        ["t1", "abandoned", "abandoned"],
        ["t2", "-", "queued"],
        ["t3", "done", "merged"],
    ]
    assert {e["ticket"] for e in factory.events(run_id, "TicketClaimed")} == {"t1", "t3"}
    [lost] = factory.events(run_id, "AttemptLost")
    assert (lost["ticket"], lost["cause"]) == ("t1", "cancelled")
    assert factory.slots() == []
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["partial"]
    assert "half.txt" not in factory.git("ls-tree", "--name-only", f"factory/{run_id}")
    assert "+half" in Path(lost["discarded"]).read_text()


def test_pr_names_the_cancelled_ticket_and_what_it_blocked(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    factory.executor.scripts["implement"] = HELLO
    factory.executor.ticket_scripts[("t1", "implement")] = AgentScript(wait_for=factory.root / "never")
    Owner(factory).then(started(factory, "implement"), "cancel", "t1")
    spec = factory.write_spec(
        "Greeting",
        "Say hello.",
        {
            "t1": ticket_text("Greet"),
            "t2": ticket_text("Greet again", ["t1"]),
            "t3": ticket_text("Wave again", ["t2"]),
            "t4": ticket_text("Wave"),
        },
    )

    run_id = run_id_of(factory.run(spec).out)

    pull = (spec / "pull_requests" / f"factory--{run_id}.md").read_text()
    assert pull.endswith(
        "Not landed:\n\n"
        "- t1 Greet: abandoned, blocking t2, t3\n"
        "- t2 Greet again: not started, blocked by t1\n"
        "- t3 Wave again: not started, blocked by t2\n"
    )


def test_cancel_takes_a_queued_ticket_out_before_it_starts(factory: Harness) -> None:
    factory.executor.scripts["implement"] = HELLO
    factory.executor.ticket_scripts[("t1", "implement")] = AgentScript(
        output=HELLO.output, wait_for=factory.root / "go", files=HELLO.files
    )
    owner = Owner(factory).then(started(factory, "implement"), "cancel", "t2")
    owner.then(lambda run_id: bool(factory.commands() and factory.commands()[0][0] == "accepted"), "cancel", "t1")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET, "t2": ticket_text("Wave")})

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    assert [r[0] for r in owner.results()] == [0, 0]
    assert [e["ticket"] for e in factory.events(run_id, "TicketClaimed")] == ["t1"]
    assert factory.workspace(run_id, "t2").exists() is False
    assert [row.split()[6] for row in factory.cli("status", run_id).out.splitlines()[2:4]] == ["abandoned"] * 2


def test_cancel_of_a_ticket_that_waits_for_the_owner_takes_its_question_back(factory: Harness) -> None:
    asking(factory)
    Owner(factory).then(lambda run_id: factory.waiting() == ["t1"], "cancel", "t1")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    assert result.code == 1, result.out
    assert factory.waiting() == []
    assert factory.git("rev-parse", f"refs/factory/{run_id}/abandoned/t1")


def test_cancel_is_rejected_for_a_merged_ticket_and_an_unknown_one(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    gate = factory.root / "go"
    factory.executor.scripts["implement"] = HELLO
    factory.executor.ticket_scripts[("t2", "implement")] = AgentScript(output=HELLO.output, wait_for=gate)
    merged = lambda run_id: bool(factory.events(run_id, "OperationFinished"))  # noqa: E731
    owner = Owner(factory).then(merged, "cancel", "t1")
    owner.then(lambda run_id: len(factory.commands()) == 1, "cancel", "t9")
    owner.then(lambda run_id: len(factory.commands()) == 2, "cancel", "t2")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET, "t2": ticket_text("Wave")})

    result = factory.run(spec)

    assert result.code == 1, result.out
    done, unknown, last = owner.results()
    assert done[0] == 1 and "Ticket t1 is already merged" in done[2]
    assert unknown[0] == 1 and "no Ticket t9 in the Run" in unknown[2]
    assert last[0] == 0


def test_override_replaces_the_last_decision_and_stops_the_work_after_it(factory: Harness) -> None:
    deciding(factory, RISK)
    factory.pipeline["decisions"]["judge"]["next"]["safe"] = "hold"
    factory.pipeline["stages"]["hold"] = {"role": "implementer", "inputs": ["ticket"], "next": {"done": "sync"}}
    factory.executor.scripts["review"] = review("two_way", "local")
    factory.executor.scripts["hold"] = AgentScript(files={"late.txt": "late\n"}, wait_for=factory.root / "never")
    owner = Owner(factory).then(started(factory, "hold"), "override", "t1", "risky")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    assert result.code == 1, result.out
    assert owner.results() == [(0, "Ticket t1 overrode judge: risky, goes to abandoned", "")]
    [lost] = factory.events(run_id, "AttemptLost")
    assert (lost["node"], lost["cause"]) == ("hold", "overridden")
    [overridden] = factory.events(run_id, "DecisionOverridden")
    assert (overridden["node"], overridden["round"], overridden["outcome"]) == ("judge", 1, "risky")
    judge = factory.run_dir(run_id) / "tickets" / "t1" / "judge" / "round-1"
    assert json.loads((judge / "decision.json").read_text())["outcome"] == "safe"
    assert json.loads((judge / "override-1.json").read_text()) == {
        "decision": "judge",
        "decider": "human",
        "outcome": "risky",
        "reasons": [],
        "overrides": "safe",
    }
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")][-2:] == ["hold", "abandoned"]
    assert factory.slots() == []


def test_override_of_a_waiting_decision_decides_it(factory: Harness) -> None:
    asking(factory)
    Owner(factory).then(lambda run_id: factory.waiting() == ["t1"], "override", "t1", "accept")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    assert result.code == 0, result.out
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    assert [e["outcome"] for e in factory.events(run_id, "DecisionOverridden")] == ["accept"]
    assert factory.waiting() == []


def test_override_sends_the_ticket_back_to_work_only_with_reasons_and_only_from_the_outcomes_of_the_decision(
    factory: Harness,
) -> None:
    asking(factory)
    owner = Owner(factory)
    owner.then(lambda run_id: factory.waiting() == ["t1"], "override", "t1", "rework")
    owner.then(lambda run_id: len(factory.commands()) == 1, "override", "t1", "ship")
    owner.then(lambda run_id: len(factory.commands()) == 2, "override", "t1", "rework", "--reason", "add a test")
    owner.then(lambda run_id: len(factory.commands()) == 3, "override", "t9", "accept")
    owner.then(lambda run_id: len(factory.commands()) == 4 and factory.waiting() == ["t1"], "override", "t1", "accept")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    rework, ship, reasoned, unknown, accepted = owner.results()
    assert rework[0] == 1 and "sends the Ticket back to work and needs reasons" in rework[2]
    assert ship[0] == 1 and "outcome 'ship' is not one of" in ship[2]
    assert reasoned[0] == 0 and "overrode judge: rework, goes to fix" in reasoned[1]
    assert unknown[0] == 1 and "no Ticket t9" in unknown[2]
    assert accepted[0] == 0


def test_override_is_rejected_for_a_ticket_without_a_decision(factory: Harness) -> None:
    factory.executor.scripts["implement"] = AgentScript(
        output=HELLO.output, files=HELLO.files, wait_for=factory.root / "go"
    )
    owner = Owner(factory).then(started(factory, "implement"), "override", "t1", "accept")
    owner.then(lambda run_id: len(factory.commands()) == 1, "cancel", "t1")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    factory.run(spec)

    [override, _] = owner.results()
    assert override[0] == 1 and "Ticket t1 has made no Decision to override" in override[2]


def test_limits_raise_the_parallelism_of_a_run_on_the_go(factory: Harness) -> None:
    gate = factory.root / "go"
    factory.executor.scripts["implement"] = AgentScript(
        files=HELLO.files, output=HELLO.output, wait_for=gate, run=["true"]
    )
    owner = Owner(factory).then(started(factory, "implement"), "limits", "--max-parallel", "2")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET, "t2": ticket_text("Wave")})
    release = factory.clock.on_sleep

    def release_once_both_run(seconds: float) -> None:
        assert release is not None
        release(seconds)
        if factory.run_ids() and len(factory.events(factory.run_ids()[-1], "AttemptStarted")) == 2:
            gate.touch()

    factory.clock.on_sleep = release_once_both_run

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    assert result.code == 0, result.out
    assert owner.results() == [(0, "Limits set: max_parallel 2", "")]
    assert [e["max_parallel"] for e in factory.events(run_id, "LimitsChanged")] == [2]


def test_limits_change_the_round_limit_of_a_stage(factory: Harness) -> None:
    fix_loop(factory, rounds={"max": 5, "then": "abandoned"})
    gate = factory.root / "go"
    factory.executor.scripts["implement"] = AgentScript(output=HELLO.output)
    factory.executor.scripts["fix"] = AgentScript(output=HELLO.output, wait_for=gate)
    owner = Owner(factory).then(started(factory, "fix"), "limits", "--rounds", "fix=2")
    release = factory.clock.on_sleep

    def open_gate_once_set(seconds: float) -> None:
        assert release is not None
        release(seconds)
        if factory.commands() and factory.commands()[0][0] == "accepted":
            gate.touch()

    factory.clock.on_sleep = open_gate_once_set
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    run_id = run_id_of(result.out)
    assert result.code == 1, result.out
    assert owner.results() == [(0, "Limits set: fix rounds 2", "")]
    [exhausted] = factory.events(run_id, "RoundsExhausted")
    assert (exhausted["node"], exhausted["rounds"]) == ("fix", 2)
    assert len([e for e in factory.events(run_id, "AttemptStarted") if e["node"] == "fix"]) == 2


def test_limits_are_rejected_for_a_stage_without_a_round_limit_a_missing_stage_and_no_values(
    factory: Harness,
) -> None:
    factory.executor.scripts["implement"] = AgentScript(
        output=HELLO.output, files=HELLO.files, wait_for=factory.root / "go"
    )
    owner = Owner(factory).then(started(factory, "implement"), "limits", "--rounds", "implement=3")
    owner.then(lambda run_id: len(factory.commands()) == 1, "limits", "--rounds", "nowhere=3")
    owner.then(lambda run_id: len(factory.commands()) == 2, "limits")
    owner.then(lambda run_id: len(factory.commands()) == 2, "limits", "--max-parallel", "0")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(
        output=HELLO.output, files=HELLO.files, wait_for=factory.root / "go"
    )
    go = factory.root / "go"
    sleep = factory.clock.on_sleep

    def release(seconds: float) -> None:
        assert sleep is not None
        sleep(seconds)
        if len(factory.commands()) == 2 and len(owner.procs) >= 4:
            go.touch()

    factory.clock.on_sleep = release

    factory.run(spec)

    plain, missing, bare, zero = owner.results()
    assert plain[0] == 1 and "implement is no Stage with a round limit" in plain[2]
    assert missing[0] == 1 and "nowhere is no Stage with a round limit" in missing[2]
    assert bare[0] == 2 and "limits needs --max-parallel or --rounds" in bare[2]
    assert zero[0] == 2 and "must be a positive integer" in zero[2]


def test_limits_cannot_raise_parallelism_past_a_merge_without_a_sync(factory: Harness) -> None:
    factory.pipeline["stages"]["implement"]["next"]["done"] = "merge"
    del factory.pipeline["operations"]["sync"]
    gate = factory.root / "go"
    factory.executor.scripts["implement"] = AgentScript(files=HELLO.files, output=HELLO.output, wait_for=gate)
    owner = Owner(factory).then(started(factory, "implement"), "limits", "--max-parallel", "2")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    sleep = factory.clock.on_sleep

    def release_once_answered(seconds: float) -> None:
        assert sleep is not None
        sleep(seconds)
        if factory.commands() and factory.commands()[0][0] != "pending":
            gate.touch()

    factory.clock.on_sleep = release_once_answered

    factory.run(spec)

    [(code, _, err)] = owner.results()
    assert code == 1 and "need a sync before merge" in err
