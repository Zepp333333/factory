import threading
from pathlib import Path

import yaml

from factory.cli import main
from harness import AgentScript, FakeClock, Harness, run_id_of


def ticket(name: str, blocked_by: list[str] | None = None) -> str:
    meta = yaml.safe_dump({"title": name, **({"blocked_by": blocked_by} if blocked_by else {})})
    return f"---\n{meta}---\nWrite {name}.txt.\n"


def writes(name: str, command: str | None = None, delay: float = 0) -> AgentScript:
    run = ([f"sleep {delay}"] if delay else []) + ([command] if command else [])
    files = {} if command else {f"{name}.txt": f"{name}\n"}
    return AgentScript(files=files, run=run, output={"summary": f"Add {name}", "handoff": "done"})


def play(factory: Harness, scripts: dict[str, AgentScript]) -> None:
    for name, script in scripts.items():
        factory.executor.ticket_scripts[(name, "implement")] = script


def timeline(factory: Harness, run_id: str) -> list[str]:
    """Lists the Attempt starts and ends and the merges of a Run in order, such as `start a`, `end a`, `merged a`."""
    marks = {"AttemptStarted": "start", "AttemptFinished": "end", "OperationFinished": "merged"}
    return [f"{marks[type]} {event['ticket']}" for type, event in factory.all_events(run_id) if type in marks]


def test_independent_tickets_run_together_but_never_more_than_max_parallel(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    spec = factory.write_spec("Files", "Three files.", {name: ticket(name) for name in "abc"})
    play(factory, {name: writes(name) for name in "abc"})

    result = factory.run(spec)

    assert result.code == 0, result.out
    steps = timeline(factory, run_id_of(result.out))
    assert steps[:2] == ["start a", "start b"]
    assert steps.index("start c") > min(steps.index("end a"), steps.index("end b"))
    assert sorted(step for step in steps if step.startswith("merged")) == ["merged a", "merged b", "merged c"]


def test_ticket_starts_after_its_blockers_merged_from_the_tip_of_the_integration_branch(factory: Harness) -> None:
    factory.project["max_parallel"] = 3
    tickets = {"a": ticket("a"), "b": ticket("b"), "c": ticket("c", ["a", "b"])}
    spec = factory.write_spec("Files", "Two files and their sum.", tickets)
    play(factory, {"a": writes("a"), "b": writes("b"), "c": writes("c", "cat a.txt b.txt > c.txt")})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    steps = timeline(factory, run_id)
    assert steps.index("start c") > max(steps.index("merged a"), steps.index("merged b"))
    integration = f"factory/{run_id}"
    assert factory.git("show", f"{integration}:c.txt") == "a\nb"
    claims = {e["ticket"]: e["base"] for e in factory.events(run_id, "TicketClaimed")}
    last_merge = [e["after"] for e in factory.events(run_id, "OperationFinished")][1]
    assert claims["a"] == claims["b"] == factory.git("rev-parse", "main")
    assert claims["c"] == last_merge


def test_ticket_blocked_by_an_abandoned_ticket_never_starts(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    spec = factory.write_spec("Files", "Two files.", {"a": ticket("a"), "b": ticket("b", ["a"])})
    play(factory, {"a": AgentScript(output={"summary": "no handoff"}), "b": writes("b")})

    result = factory.run(spec)

    assert result.code == 1
    assert [r.stage for r in factory.executor.requests] == ["implement"]
    assert "b" not in [e["ticket"] for e in factory.events(run_id_of(result.out), "TicketClaimed")]


def test_slot_cap_of_the_machine_holds_back_attempts_of_independent_tickets(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    factory.machine_config["slots"] = 1
    spec = factory.write_spec("Files", "Two files.", {name: ticket(name) for name in "ab"})
    play(factory, {name: writes(name) for name in "ab"})

    result = factory.run(spec)

    assert result.code == 0, result.out
    steps = [step for step in timeline(factory, run_id_of(result.out)) if not step.startswith("merged")]
    assert steps == ["start a", "end a", "start b", "end b"]


def test_runs_on_one_machine_share_the_slot_cap(factory: Harness, tmp_path: Path) -> None:
    factory.machine_config["slots"] = 1
    specs = []
    for name in "ab":
        folder = tmp_path / f"spec-{name}"
        (folder / "tickets").mkdir(parents=True)
        (folder / "spec.md").write_text(f"# {name}\n\nWrite {name}.\n")
        (folder / "tickets" / f"{name}.md").write_text(ticket(name))
        specs.append(folder)
    play(factory, {name: writes(name, delay=0.3) for name in "ab"})
    codes: list[int] = []

    def run(spec: Path) -> None:
        codes.append(main(["run", str(spec), "--project", str(factory.project_file)], factory.machine(FakeClock())))

    threads = [threading.Thread(target=run, args=(spec,)) for spec in specs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert codes == [0, 0]
    marks = {"AttemptStarted": "start", "AttemptFinished": "end"}
    steps = [marks[type] for _, type, _ in factory.all_runs_events() if type in marks]
    assert steps == ["start", "end", "start", "end"]


def test_run_records_the_final_pull_request_in_the_tracker_folder(factory: Harness) -> None:
    factory.project["max_parallel"] = 2
    spec = factory.write_spec("Files", "Two files.", {"a": ticket("a"), "b": ticket("b", ["a"])})
    play(factory, {name: writes(name) for name in "ab"})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    path = spec / "pull_requests" / f"factory--{run_id}.md"
    text = path.read_text()
    assert text.startswith(f"# Files\n\nFrom `factory/{run_id}` into `main`.\n")
    assert "- a a\n- b b\n" in text
    assert f"Pull request: {path}" in result.out
