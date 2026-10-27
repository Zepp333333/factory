import json
import subprocess
import time
from pathlib import Path
from typing import Any

from factory.commands import submit_decide
from factory.journal import Journal
from harness import AgentScript, CliResult, FakeTty, Harness, run_id_of, ticket_text

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
HELLO = AgentScript(files={"hello.txt": "hello\n"}, output={"summary": "Add hello.txt", "handoff": "done"})
FIXED = AgentScript(files={"fixed.txt": "fixed\n"}, output={"summary": "Fix it", "handoff": "fixed"})
OUTCOMES = {"accept": "Land the change", "rework": "Send it back to work", "abandon": "Drop the Ticket"}
GIVE_UP = 20000


def asking(factory: Harness) -> None:
    """Puts a human Decision `judge` after `implement`, with `rework` leading to a Stage `fix` that reads the cause."""
    pipeline = factory.pipeline
    pipeline["stages"]["implement"]["next"]["done"] = "judge"
    pipeline["stages"]["fix"] = {
        "role": "implementer",
        "rounds": {"max": 3},
        "inputs": ["spec", "ticket", "cause", {"stage": ["implement", "fix"], "optional": True}],
        "next": {"done": "judge"},
    }
    pipeline["decisions"] = {
        "judge": {
            "decider": {"kind": "human"},
            "outcomes": OUTCOMES,
            "inputs": [{"stage": ["implement", "fix"]}],
            "next": {"accept": "sync", "rework": {"to": "fix", "reset_rounds": ["fix"]}, "abandon": "abandoned"},
        }
    }
    factory.executor.scripts["implement"] = HELLO
    factory.executor.scripts["fix"] = FIXED


class Asker:
    """An Asker answers the cards of a Run from other processes, as the owner would from other terminals.

    `answers` lists the `factory decide` arguments after the Run id, one list per card in the order the cards appear.
    The Asker starts them one after another from the Run's sleep, and never before `cards` cards are waiting.
    Its answers to one card go out one at a time unless `together` is set, which starts them all while the Run waits
    in the sleep, until every one is in the inbox.
    """

    def __init__(
        self, factory: Harness, answers: list[list[list[str]]], cards: int = 1, together: bool = False
    ) -> None:
        self.factory = factory
        self.answers = answers
        self.cards = cards
        self.together = together
        self.procs: list[subprocess.Popen[str]] = []
        self.sleeps = 0
        self.seen: dict[str, Any] = {}
        self._card = 0
        self._step = 0
        factory.clock.on_sleep = self.on_sleep

    def on_sleep(self, seconds: float) -> None:
        self.sleeps += 1
        assert self.sleeps < GIVE_UP, "the Run never asked"
        if not self.factory.run_ids() or self._card >= len(self.answers):
            return
        if self.procs and self.procs[-1].poll() is None:
            return
        run_id = self.factory.run_ids()[-1]
        cards = sorted(self.factory.run_dir(run_id).glob("tickets/*/judge/round-*/question.md"))
        if len(cards) < max(self._card + 1, self.cards):
            return
        if not self.seen:
            self.seen = {"slots": self.factory.slots(), "sandboxes": self.factory.sbx.state()["sandboxes"]}
        answers = self.answers[self._card]
        if self.together:
            self.procs += [self.factory.spawn("decide", run_id, *argv) for argv in answers]
            deadline = time.monotonic() + 30
            while len(self.factory.commands()) < len(answers) and time.monotonic() < deadline:
                time.sleep(0.01)
            self._card += 1
            return
        self.procs.append(self.factory.spawn("decide", run_id, *answers[self._step]))
        self._step += 1
        if self._step == len(answers):
            self._card, self._step = self._card + 1, 0

    def results(self) -> list[CliResult]:
        out = []
        for proc in self.procs:
            stdout, stderr = proc.communicate(timeout=30)
            out.append(CliResult(proc.returncode, stdout, stderr))
        return out


def decision_file(factory: Harness, run_id: str, round: int = 1, ticket: str = "t1") -> dict[str, Any]:
    path = factory.run_dir(run_id) / "tickets" / ticket / "judge" / f"round-{round}" / "decision.json"
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def test_decide_answers_a_human_decision_and_prints_the_outcome(factory: Harness) -> None:
    asking(factory)
    asker = Asker(factory, [[["t1", "accept"]]])
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    [answer] = asker.results()
    assert (answer.code, answer.out) == (0, "Ticket t1 decided judge: accept, goes to sync\n")
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    assert decision_file(factory, run_id) == {
        "decision": "judge",
        "decider": "human",
        "outcome": "accept",
        "reasons": [],
    }
    [(state, text, payload)] = factory.commands()
    assert (state, text, payload["source"]) == ("accepted", answer.out.strip(), "cli")
    assert "\x07" in result.out
    assert "waiting for you at judge" in result.out
    assert all(r.stage != "judge" for r in factory.executor.requests)


def test_entering_a_human_decision_renders_the_question_card(factory: Harness) -> None:
    asking(factory)
    Asker(factory, [[["t1", "accept"]]])
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    run_id = run_id_of(factory.run(spec).out)

    card = (factory.run_dir(run_id) / "tickets" / "t1" / "judge" / "round-1" / "question.md").read_text()
    [implemented] = (factory.run_dir(run_id) / "tickets" / "t1" / "implement").glob("round-1/attempt-1/output.json")
    assert "# Decision judge of Ticket t1: Greet" in card
    assert f"factory decide {run_id} t1 <outcome> --reason <why>" in card
    assert f"- stage implement: {implemented}" in card
    assert "- accept: Land the change" in card
    assert "- rework (needs reasons): Send it back to work" in card
    assert "- abandon: Drop the Ticket" in card
    assert "- escalate" in card


def test_rework_needs_reasons_and_the_next_stage_gets_them_as_its_cause(factory: Harness) -> None:
    asking(factory)
    asker = Asker(
        factory,
        [
            [["t1", "rework"], ["t1", "rework", "--reason", "greet louder", "--reason", "add a test"]],
            [["t1", "accept"]],
        ],
    )
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    bare, reworked, accepted = asker.results()
    assert (bare.code, bare.err) == (
        1,
        "factory: answer rejected: outcome rework sends the Ticket back to work and needs reasons\n",
    )
    assert (reworked.code, accepted.code) == (0, 0)
    assert decision_file(factory, run_id)["reasons"] == ["greet louder", "add a test"]
    fix = next(r for r in factory.executor.requests if r.stage == "fix")
    [cause] = [i for i in json.loads(fix.brief.read_text())["inputs"] if i["source"] == "cause"]
    assert json.loads(Path(cause["path"]).read_text())["reasons"] == ["greet louder", "add a test"]
    assert factory.git("show", f"factory/{run_id}:fixed.txt") == "fixed"
    assert [state for state, _, _ in factory.commands()] == ["rejected", "accepted", "accepted"]


def test_an_outcome_outside_the_list_is_rejected(factory: Harness) -> None:
    asking(factory)
    asker = Asker(factory, [[["t1", "ship-it"], ["t1", "accept"]]])
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    wrong, right = asker.results()
    assert wrong.code == 1
    assert "outcome 'ship-it' is not one of abandon, accept, rework, escalate" in wrong.err
    assert right.code == 0


def test_the_first_valid_answer_is_accepted_and_the_second_is_rejected_as_already_decided(factory: Harness) -> None:
    asking(factory)
    asker = Asker(factory, [[["t1", "accept"], ["t1", "accept"]]], together=True)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    answers = asker.results()
    assert sorted(a.code for a in answers) == [0, 1]
    assert [a.err for a in answers if a.code == 1] == ["factory: answer rejected: already decided\n"]
    assert [state for state, _, _ in factory.commands()] == ["accepted", "rejected"]
    run_id = run_id_of(result.out)
    assert len(factory.events(run_id, "DecisionDecided")) == 1


def test_waiting_for_the_owner_holds_no_slot_and_the_other_tickets_go_on(factory: Harness) -> None:
    asking(factory)
    factory.machine_config = {"slots": 1}
    factory.project["max_parallel"] = 2
    asker = Asker(factory, [[["t1", "accept"]], [["t2", "accept"]]], cards=2)
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("one"), "t2": ticket_text("two")})
    factory.executor.scripts["implement"] = [
        AgentScript(files={"one.txt": "1\n"}, output={"handoff": "done"}),
        AgentScript(files={"two.txt": "2\n"}, output={"handoff": "done"}),
    ]

    result = factory.run(spec)

    assert result.code == 0, result.out
    assert asker.seen["slots"] == []
    assert [r.stage for r in factory.executor.requests] == ["implement", "implement"]
    assert [a.code for a in asker.results()] == [0, 0]


def test_the_sandbox_of_a_waiting_ticket_is_stopped_and_started_again_by_the_answer(factory: Harness) -> None:
    asking(factory)
    factory.project["sandbox"] = {"kind": "sbx"}
    asker = Asker(factory, [[["t1", "accept"]]])
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert asker.seen["sandboxes"][f"{run_id}-t1"]["status"] == "stopped"
    assert factory.sbx.calls("stop") == [[f"{run_id}-t1"]]
    starts = [c for c in factory.sbx.calls("exec") if c == ["-w", "/", f"{run_id}-t1", "true"]]
    assert len(starts) == 2, "the Sandbox starts for implement and again after the answer"
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"


def test_an_answer_typed_in_the_terminal_of_the_run_is_the_same_command(factory: Harness) -> None:
    asking(factory)
    tty = factory.stdin = FakeTty()
    tty.type("rework\naccept\n")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    tty.close()
    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert "# Decision judge of Ticket t1: Greet" in result.out
    assert "t1: answer rejected, outcome rework sends the Ticket back to work and needs reasons" in result.out
    assert "Ticket t1 decided judge: accept, goes to sync" in result.out
    assert [(state, payload["source"], payload["outcome"]) for state, _, payload in factory.commands()] == [
        ("rejected", "terminal", "rework"),
        ("accepted", "terminal", "accept"),
    ]
    assert factory.git("show", f"factory/{run_id}:hello.txt") == "hello"
    assert "\x07" in result.out


def test_a_terminal_that_is_no_tty_is_not_asked(factory: Harness) -> None:
    asking(factory)
    asker = Asker(factory, [[["t1", "abandon"]]])
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert "# Decision judge" not in result.out
    [answer] = asker.results()
    assert answer.code == 0
    assert factory.cli("status", run_id_of(result.out)).out.splitlines()[2].split()[-1] == "abandoned"


def test_a_line_typed_for_a_card_decided_elsewhere_is_dropped_and_not_sent_for_the_next_card(
    factory: Harness,
) -> None:
    asking(factory)
    factory.machine_config = {"slots": 1}
    factory.project["max_parallel"] = 2
    tty = factory.stdin = FakeTty()
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("one"), "t2": ticket_text("two")})
    factory.executor.scripts["implement"] = [
        AgentScript(files={"one.txt": "1\n"}, output={"handoff": "done"}),
        AgentScript(files={"two.txt": "2\n"}, output={"handoff": "done"}),
    ]
    procs: list[subprocess.Popen[str]] = []
    sleeps = 0

    def on_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        assert sleeps < GIVE_UP, "the Run never asked"
        waiting = factory.waiting()
        if not procs and waiting == ["t1", "t2"]:
            procs.append(factory.spawn("decide", factory.run_ids()[-1], "t1", "accept"))
            deadline = time.monotonic() + 30
            while not factory.commands() and time.monotonic() < deadline:
                time.sleep(0.01)
            # The next tick decides t1 from the CLI, and the owner types an answer for t1 before seeing the next card.
            tty.type_later("abandon\n", 2)
        elif len(procs) == 1 and waiting == ["t2"]:
            procs.append(factory.spawn("decide", factory.run_ids()[-1], "t2", "accept"))

    factory.clock.on_sleep = on_sleep

    result = factory.run(spec)

    tty.close()
    assert result.code == 0, result.out
    assert [proc.wait(timeout=30) for proc in procs] == [0, 0]
    assert [(state, payload["ticket"], payload["source"]) for state, _, payload in factory.commands()] == [
        ("accepted", "t1", "cli"),
        ("accepted", "t2", "cli"),
    ]
    assert "dropped what was typed for the Decision of t1, which was decided elsewhere" in result.out


def test_an_answer_sent_while_the_ticket_waited_for_none_is_rejected_when_a_decision_comes(
    factory: Harness,
) -> None:
    asking(factory)
    sent: list[int] = []
    asker = Asker(factory, [[["t1", "accept"]]])
    answer = asker.on_sleep

    def on_sleep(seconds: float) -> None:
        if not sent and factory.waiting() == ["t1"]:
            # The answer was put in the inbox by a `decide` that found no waiting Decision.
            journal = Journal(factory.home / "factory.db", factory.clock)
            sent.append(submit_decide(journal, factory.run_ids()[-1], "t1", "abandon", [], "cli", None))
            journal.close()
            return
        answer(seconds)

    factory.clock.on_sleep = on_sleep
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    assert [a.code for a in asker.results()] == [0]
    stale, accepted = factory.commands()
    assert (stale[0], stale[1]) == (
        "rejected",
        "the answer names no Decision, as Ticket t1 waited for none when it was sent",
    )
    assert accepted[0] == "accepted"
    assert factory.git("show", f"factory/{run_id_of(result.out)}:hello.txt") == "hello"


def owner_acts_while_waiting(factory: Harness, act: Any, answers: list[list[str]]) -> list[subprocess.Popen[str]]:
    """Runs `act(workspace)` once the Ticket waits, then answers with each of `answers` after the previous one ended."""
    procs: list[subprocess.Popen[str]] = []
    sleeps = 0

    def on_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        assert sleeps < GIVE_UP, "the Run never asked"
        if factory.waiting() != ["t1"] or (procs and procs[-1].poll() is None):
            return
        run_id = factory.run_ids()[-1]
        if not procs:
            act(factory.workspace(run_id, "t1"))
        if len(procs) < len(answers):
            procs.append(factory.spawn("decide", run_id, *answers[len(procs)]))

    factory.clock.on_sleep = on_sleep
    return procs


def owner_git(workspace: Path, *args: str) -> str:
    owner = ["-c", "user.name=Owner", "-c", "user.email=owner@example.com"]
    return subprocess.run(
        ["git", *owner, *args], cwd=workspace, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_the_owners_commits_and_leftovers_reach_the_next_stage_as_a_range_with_the_human_trailer(
    factory: Harness,
) -> None:
    asking(factory)

    def edit(workspace: Path) -> None:
        (workspace / "own.txt").write_text("committed\n")
        owner_git(workspace, "add", "own.txt")
        owner_git(workspace, "commit", "-m", "Owner commit")
        (workspace / "left.txt").write_text("left over\n")

    procs = owner_acts_while_waiting(factory, edit, [["t1", "rework", "--reason", "go on"], ["t1", "accept"]])
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out
    assert [p.wait(timeout=30) for p in procs] == [0, 0]
    run_id = run_id_of(result.out)
    fix = next(r for r in factory.executor.requests if r.stage == "fix")
    [implemented] = [i for i in json.loads(fix.brief.read_text())["inputs"] if i.get("node") == "implement"]
    commits = factory.git("log", "--format=%H", f"{implemented['head_before']}..{implemented['head_after']}")
    assert len(commits.splitlines()) == 3, "the Stage boundary commit, the owner's commit and the leftovers"
    leftovers = factory.git("log", "-1", "--format=%B", implemented["head_after"])
    assert "Factory-Actor: human" in leftovers
    assert "Factory-Ticket: t1" in leftovers
    assert factory.git("show", f"factory/{run_id}:own.txt") == "committed"
    assert factory.git("show", f"factory/{run_id}:left.txt") == "left over"
    assert "Factory-Actor" not in factory.git("log", "-1", "--format=%B", implemented["head_before"] + "^{commit}")


def test_an_answer_is_rejected_when_the_owner_rewrote_the_history_of_the_workspace(factory: Harness) -> None:
    asking(factory)
    saved: list[str] = []

    def rewrite(workspace: Path) -> None:
        saved.append(owner_git(workspace, "rev-parse", "HEAD"))
        owner_git(workspace, "reset", "--hard", "HEAD~1")

    procs = owner_acts_while_waiting(factory, rewrite, [["t1", "accept"]])
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    on_sleep = factory.clock.on_sleep
    assert on_sleep is not None

    def restore(seconds: float) -> None:
        if procs and procs[0].poll() is not None and saved:
            run_id = factory.run_ids()[-1]
            owner_git(factory.workspace(run_id, "t1"), "reset", "--hard", saved.pop())
            procs.append(factory.spawn("decide", run_id, "t1", "accept"))
        elif not saved or not procs:
            on_sleep(seconds)

    factory.clock.on_sleep = restore

    result = factory.run(spec)

    assert result.code == 0, result.out
    rejected, accepted = [p.communicate(timeout=30) for p in procs]
    assert procs[0].returncode == 1
    assert "answer rejected: the history of the Workspace was rewritten while the Decision waited" in rejected[1]
    assert procs[1].returncode == 0, accepted
