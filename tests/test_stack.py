"""Scenarios of the `stack` landing, which opens one PR per merged Ticket."""

from harness import AgentScript, Harness, run_id_of, ticket_text
from test_describer import APPEND, BODY, DESCRIPTION, with_describer
from test_recovery import STALE, crashed_run, killed_runs_run_no_handler  # noqa: F401
from test_tracker_run import Tracker, publish_with_closed_tickets, tracker  # noqa: F401


def greet(factory: Harness) -> None:
    factory.project["landing"] = "stack"
    output = {"summary": "Write a file", "handoff": "written"}
    factory.executor.ticket_scripts[("28", "implement")] = AgentScript(files={"hello.txt": "hello\n"}, output=output)
    factory.executor.ticket_scripts[("30", "implement")] = AgentScript(files={"bye.txt": "bye\n"}, output=output)


def test_stack_opens_one_pr_per_merged_ticket_each_into_the_layer_below_and_waits_for_ci_on_the_top_one(
    tracker: Tracker,  # noqa: F811
) -> None:
    factory = tracker.factory
    url = publish_with_closed_tickets(tracker)
    greet(factory)
    with_describer(factory)
    factory.executor.scripts["implement"] = AgentScript(output={"summary": "x", "handoff": "y"})
    factory.executor.scripts["describe"] = DESCRIPTION

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    first, second = f"factory/{run_id}--1-greet", f"factory/{run_id}--2-greet-again"
    assert factory.git("rev-parse", second) == factory.git("rev-parse", f"factory/{run_id}")
    assert factory.git("ls-tree", "-r", "--name-only", first).splitlines()[-1] == "hello.txt"
    assert "bye.txt" not in factory.git("ls-tree", "-r", "--name-only", first)
    assert factory.git("rev-parse", f"{second}~1") == factory.git("rev-parse", first)
    assert set(factory.remote_branches()) == {first, second}

    low, high = tracker.pulls()
    assert (low["head"], low["base"]) == (first, "main")
    assert (high["head"], high["base"]) == (second, first)
    assert "Closes #28\n" in low["body"] and "Closes #30" not in low["body"]
    assert "Closes #30\n" in high["body"] and "Closes #28" not in high["body"]
    for pull, mark in (
        (low, "1. Greet (`" + first + "`) **<- this PR**"),
        (high, "2. Greet again (`" + second + "`) **<- this PR**"),
    ):
        assert mark in pull["body"]
        assert "bottom up, with a merge commit only" in pull["body"]
        assert "delete the branch after each merge" in pull["body"]
    assert f"1. Greet (`{first}`)\n2. Greet again (`{second}`) **<- this PR**" in high["body"]

    briefs = [started["brief"] for started in factory.events(run_id, "DescriberStarted")]
    assert [(b["layer"], b["base_ref"], b["integration_branch"]) for b in briefs] == [
        (1, "main", first),
        (2, first, second),
    ]
    assert briefs[1]["base"] == factory.git("rev-parse", first)
    assert {entry.get("ticket") for entry in briefs[0]["inputs"]} == {None, "28"}

    [started] = factory.events(run_id, "CiStarted")
    assert started["url"].endswith("/2")
    assert started["commit"] == factory.git("rev-parse", second)
    assert BODY.split("\n")[0] in low["body"]


def test_stack_without_a_remote_keeps_one_numbered_pr_file_per_layer(factory: Harness) -> None:
    factory.project["landing"] = "stack"
    spec = factory.write_spec(
        "Pair",
        "Two.",
        {"t1": "---\ntitle: One\n---\nFirst.\n", "t2": "---\ntitle: Two\nblocked_by: [t1]\n---\nSecond.\n"},
    )
    factory.executor.scripts["implement"] = AgentScript(
        run=["echo line >> log.txt"], output={"summary": "s", "handoff": "h"}
    )
    factory.remove_remotes()

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    names = sorted(path.name for path in (spec / "pull_requests").glob("*.md"))
    assert names == [f"01-factory--{run_id}--1-one.md", f"02-factory--{run_id}--2-two.md"]
    assert (
        f"From `factory/{run_id}--2-two` into `factory/{run_id}--1-one`."
        in (spec / "pull_requests" / names[1]).read_text()
    )
    assert result.out.splitlines()[-1] == f"Pull request: {spec / 'pull_requests' / names[1]}"


def test_check_rejects_a_landing_that_is_neither_single_nor_stack(factory: Harness) -> None:
    factory.project["landing"] = "pile"

    result = factory.check()

    assert result.code == 2
    assert "landing: must be one of single, stack" in result.err
    factory.project["landing"] = "stack"
    assert factory.check().code == 0


def test_a_stacked_run_killed_in_describe_keeps_the_text_of_the_layer_the_describer_wrote_before(
    factory: Harness,
) -> None:
    with_describer(factory)
    factory.project["landing"] = "stack"
    stuck = AgentScript(output=DESCRIPTION.output, wait_for=factory.root / "go")
    factory.executor.scripts["describe"] = [DESCRIPTION, stuck, DESCRIPTION]
    spec = factory.write_spec("Pair", "Two.", {"t1": ticket_text("One"), "t2": ticket_text("Two", ["t1"])})
    factory.executor.ticket_scripts[("t2", "implement")] = AgentScript(files={"two.txt": "two\n"}, output=APPEND.output)
    run_id = crashed_run(factory, spec, lambda run_id: len(factory.events(run_id, "DescriberStarted")) == 2)
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    described = [event["layer"] for event in factory.events(run_id, "PullRequestDescribed")]
    assert described.count(1) == 1
    assert (factory.run_dir(run_id) / "pull_request-1.md").read_text().startswith(BODY.strip())
