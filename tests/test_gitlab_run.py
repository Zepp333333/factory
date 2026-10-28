import sqlite3

import pytest

from harness import AgentScript, CliResult, Harness, run_id_of
from test_recovery import STALE, crashed_run, killed_runs_run_no_handler

APPEND = AgentScript(run=["echo line >> log.txt"], output={"summary": "Append a line", "handoff": "appended"})

PROJECT = "group/sub/project"


def front(*blockers: int | str, rest: str = "") -> str:
    return f"---\nblocked_by: [{', '.join(map(str, blockers))}]\n---\n{rest}"


def publish(factory: Harness, host: str = "gitlab.example.com") -> str:
    """Publishes Spec #21 with Tasks #28 and #30, where #28 waits on the closed #27 and #30 on #28 by front matter."""
    factory.glab.issue(27, "Lay the ground", state="closed", host=host)
    factory.glab.issue(28, "Greet", front(27, rest="Write hello.txt."), host=host)
    factory.glab.issue(30, "Greet again", front(28, rest="Write bye.txt."), host=host)
    return factory.glab.issue(21, "Greeting", "Say hello.", kind="Issue", children=[28, 30], host=host)


def assert_refused(factory: Harness, result: CliResult, before: dict[str, str]) -> None:
    assert result.code == 2
    assert "started" not in result.out
    assert factory.branches() == before
    assert not (factory.home / "factory.db").exists() or not runs(factory)


def runs(factory: Harness) -> list[str]:
    with sqlite3.connect(factory.home / "factory.db") as connection:
        return [run_id for (run_id,) in connection.execute("SELECT id FROM runs")]


@pytest.mark.parametrize("host", ["gitlab.example.com", "gitlab.com"])
def test_run_reads_the_spec_its_child_tasks_and_their_front_matter_blockers_into_the_snapshot_shape(
    factory: Harness, host: str
) -> None:
    url = publish(factory, host)
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [snapshotted] = factory.events(run_id, "SpecSnapshotted")
    assert snapshotted["snapshot"] == {
        "ref": url,
        "title": "Greeting",
        "body": "Say hello.",
        "tickets": [
            {"id": "28", "title": "Greet", "body": "Write hello.txt.", "blocked_by": []},
            {"id": "30", "title": "Greet again", "body": "Write bye.txt.", "blocked_by": ["28"]},
        ],
    }
    run_dir = factory.run_dir(run_id)
    assert (run_dir / "spec.md").read_text() == "# Greeting\n\nSay hello.\n"
    assert (run_dir / "tickets" / "30" / "ticket.md").read_text() == "# Greet again\nBlocked by: 28\n\nWrite bye.txt.\n"
    claimed = [event["branch"] for event in factory.events(run_id, "TicketClaimed")]
    assert claimed == [f"factory/{run_id}--28", f"factory/{run_id}--30"]
    assert {call[3] for call in factory.glab.calls()} == {host}


def test_native_blocking_links_join_the_front_matter(factory: Harness) -> None:
    factory.glab.set(linked_items=True)
    factory.glab.issue(28, "Greet", "Write hello.txt.")
    factory.glab.issue(29, "Wave", "Write wave.txt.")
    factory.glab.issue(30, "Greet again", front(29, rest="Write bye.txt."), blocked_by=[28])
    url = factory.glab.issue(21, "Greeting", kind="Issue", children=[28, 29, 30])
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    [snapshotted] = factory.events(run_id_of(result.out), "SpecSnapshotted")
    assert {t["id"]: t["blocked_by"] for t in snapshotted["snapshot"]["tickets"]} == {
        "28": [],
        "29": [],
        "30": ["29", "28"],
    }


def test_a_closed_native_blocker_counts_as_done_and_an_open_one_in_another_project_stops_the_run(
    factory: Harness,
) -> None:
    factory.glab.set(linked_items=True)
    factory.glab.issue(5, "Elsewhere", state="closed", project="other/project")
    factory.glab.issue(28, "Greet", blocked_by=["other/project#5"])
    url = factory.glab.issue(21, "Greeting", kind="Issue", children=[28])
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    [snapshotted] = factory.events(run_id_of(result.out), "SpecSnapshotted")
    assert snapshotted["snapshot"]["tickets"][0]["blocked_by"] == []

    factory.glab.issue(6, "Elsewhere too", project="other/project")
    factory.glab.issue(29, "Wave", blocked_by=["other/project#6"])
    url = factory.glab.issue(22, "Waving", kind="Issue", children=[29])
    before = factory.branches()

    result = factory.run(url)

    assert result.code == 2
    assert factory.branches() == before
    assert result.err == "factory: #29 Wave is blocked by open #6 of other/project, which this Run does not take\n"


def test_tickets_flag_takes_only_the_listed_child_tasks(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url, "--tickets", "28")

    assert result.code == 0, result.out + result.err
    [snapshotted] = factory.events(run_id_of(result.out), "SpecSnapshotted")
    assert [ticket["id"] for ticket in snapshotted["snapshot"]["tickets"]] == ["28"]


def test_tickets_flag_with_a_number_that_is_not_a_child_task_stops_the_run(factory: Harness) -> None:
    url = publish(factory)
    factory.glab.issue(40, "Elsewhere", kind="Issue")
    before = factory.branches()

    result = factory.run(url, "--tickets", "28,40")

    assert_refused(factory, result, before)
    assert result.err == f"factory: #40 is not a child Task of {url}\n"


def test_closed_child_task_in_the_tickets_flag_stops_the_run(factory: Harness) -> None:
    factory.glab.issue(28, "Greet")
    factory.glab.issue(29, "Already done", state="closed")
    url = factory.glab.issue(21, "Greeting", kind="Issue", children=[28, 29])
    before = factory.branches()

    result = factory.run(url, "--tickets", "28,29")

    assert_refused(factory, result, before)
    assert result.err == "factory: #29 is closed, so this Run cannot take it\n"


def test_run_without_the_flag_skips_closed_and_non_task_children(factory: Harness) -> None:
    factory.glab.issue(28, "Greet")
    factory.glab.issue(29, "Already done", state="closed")
    factory.glab.issue(31, "A sub-issue", kind="Issue")
    url = factory.glab.issue(21, "Greeting", kind="Issue", children=[28, 29, 31])
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url)

    [snapshotted] = factory.events(run_id_of(result.out), "SpecSnapshotted")
    assert [ticket["id"] for ticket in snapshotted["snapshot"]["tickets"]] == ["28"]


def test_spec_with_no_open_child_tasks_stops_the_run(factory: Harness) -> None:
    factory.glab.issue(29, "Already done", state="closed")
    url = factory.glab.issue(21, "Greeting", kind="Issue", children=[29])
    before = factory.branches()

    result = factory.run(url)

    assert_refused(factory, result, before)
    assert result.err == f"factory: {url} has no open child Tasks\n"


def test_open_blocker_outside_the_selection_stops_the_run_before_it_starts(factory: Harness) -> None:
    url = publish(factory)
    before = factory.branches()

    result = factory.run(url, "--tickets", "30")

    assert_refused(factory, result, before)
    assert result.err == "factory: #30 Greet again is blocked by open #28, which this Run does not take\n"


def test_closed_blocker_that_is_not_a_child_task_counts_as_done(factory: Harness) -> None:
    factory.glab.issue(5, "Elsewhere", state="closed")
    factory.glab.issue(28, "Greet", front(5, "'#27'", rest="Write hello.txt."))
    factory.glab.issue(27, "Lay the ground", state="closed")
    url = factory.glab.issue(21, "Greeting", kind="Issue", children=[28])
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    [snapshotted] = factory.events(run_id_of(result.out), "SpecSnapshotted")
    assert snapshotted["snapshot"]["tickets"] == [
        {"id": "28", "title": "Greet", "body": "Write hello.txt.", "blocked_by": []}
    ]


@pytest.mark.parametrize(
    ("description", "message"),
    [
        ("---\nblocked_by: [1\n---\nWrite.", "front matter is not valid YAML"),
        ("---\nblocked_by: 27\n---\nWrite.", "blocked_by must be a list of issue numbers"),
        ("---\nblocked_by: [soon]\n---\nWrite.", "blocked_by names no issue: 'soon'"),
        ("---\nblocked_by: [404]\n---\nWrite.", "blocked_by names no issue: #404"),
    ],
)
def test_malformed_front_matter_or_a_blocker_that_names_no_issue_stops_the_run_with_the_tickets_name(
    factory: Harness, description: str, message: str
) -> None:
    factory.glab.issue(28, "Greet", description)
    url = factory.glab.issue(21, "Greeting", kind="Issue", children=[28])
    before = factory.branches()

    result = factory.run(url)

    assert_refused(factory, result, before)
    assert result.err.startswith("factory: #28 Greet: ")
    assert message in result.err


def test_gitlab_access_error_stops_the_run_before_it_starts(factory: Harness) -> None:
    url = publish(factory)
    factory.glab.set(denied=True)
    before = factory.branches()

    result = factory.run(url)

    assert_refused(factory, result, before)
    assert result.err == "factory: glab api failed: glab: 401 Unauthorized\n"


def test_unknown_host_or_project_or_issue_stops_the_run_before_it_starts(factory: Harness) -> None:
    publish(factory)
    before = factory.branches()

    for link, message in [
        ("https://nowhere.example.com/group/sub/project/-/issues/21", "glab api failed: Get "),
        ("https://gitlab.example.com/group/other/-/issues/21", "gitlab.example.com has no project group/other"),
        (f"https://gitlab.example.com/{PROJECT}/-/issues/404", f"is not an issue of {PROJECT}"),
    ]:
        result = factory.run(link)

        assert_refused(factory, result, before)
        assert message in result.err


def test_missing_glab_stops_the_run_before_it_starts(factory: Harness) -> None:
    url = publish(factory)
    factory.glab.command = (str(factory.root / "no-glab"),)
    before = factory.branches()

    result = factory.run(url)

    assert_refused(factory, result, before)
    assert result.err.startswith("factory: cannot run glab: ")


def test_work_items_link_is_a_gitlab_link_too(factory: Harness) -> None:
    publish(factory)
    factory.executor.scripts["implement"] = APPEND

    result = factory.run(f"https://gitlab.example.com/{PROJECT}/-/work_items/21")

    assert result.code == 0, result.out + result.err


@pytest.mark.usefixtures(killed_runs_run_no_handler.__name__)
def test_finish_rebuilds_the_gitlab_tracker_from_the_specs_link(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = [APPEND]
    factory.executor.ticket_scripts[("30", "implement")] = [AgentScript(wait_for=factory.root / "never")]
    run_id = crashed_run(factory, url, lambda run_id: len(factory.events(run_id, "AttemptStarted")) == 2)
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("finish", run_id)

    assert result.code == 0, result.out + result.err
    [failed] = factory.events(run_id, "LandingFailed")
    assert failed["reason"] == f"no git remote of {factory.repo} points at https://gitlab.example.com/{PROJECT}.git"
    assert "  glab mr create --repo gitlab.example.com/group/sub/project --source-branch" in result.out
