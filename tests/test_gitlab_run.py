import sqlite3
from typing import Any

import pytest

from harness import AgentScript, CliResult, Harness, run_id_of
from test_recovery import STALE, crashed_run, killed_runs_run_no_handler

APPEND = AgentScript(run=["echo line >> log.txt"], output={"summary": "Append a line", "handoff": "appended"})

PROJECT = "group/sub/project"
MR_REPO = f"gitlab.example.com/{PROJECT}"

# What a Ticket of a snapshot holds that these tests do not set.
TICKET_FIELDS: dict[str, list[str]] = {"blocked_by": [], "labels": [], "blocked_outside": []}


def front(*blockers: int | str, rest: str = "") -> str:
    return f"---\nblocked_by: [{', '.join(map(str, blockers))}]\n---\n{rest}"


def publish(factory: Harness, host: str = "gitlab.example.com") -> str:
    """Publishes Spec #21 with Tasks #28 and #30, where #28 waits on the closed #27 and #30 on #28 by front matter."""
    if host != "gitlab.example.com":
        factory.git("remote", "add", host, f"https://{host}/{PROJECT}.git")
    factory.glab.issue(27, "Lay the ground", state="closed", host=host)
    factory.glab.issue(28, "Greet", front(27, rest="Write hello.txt."), host=host)
    factory.glab.issue(30, "Greet again", front(28, rest="Write bye.txt."), host=host)
    return factory.glab.issue(21, "Greeting", "Say hello.", kind="Issue", children=[28, 30], host=host)


def landing(factory: Harness) -> str:
    """Publishes the Spec and leaves `origin` as the only GitLab remote of its project, pushing to the bare repo."""
    ssh = f"git@gitlab.example.com:{PROJECT}.git"
    factory.git("remote", "remove", "gitlab")
    factory.git("remote", "set-url", "origin", ssh)
    return publish(factory)


def part_of(spec: int, *blockers: int, rest: str = "Write it.") -> str:
    """Returns a description that ties a Ticket to Spec `spec` by leading lines, as a Free-tier project does."""
    blocked = f"Blocked by: {', '.join(f'#{ref}' for ref in blockers)}\n" if blockers else ""
    return f"Part of #{spec}\n{blocked}\n## What to build\n{rest}"


def assert_refused(factory: Harness, result: CliResult, before: dict[str, str]) -> None:
    assert result.code == 2
    assert "started" not in result.out
    assert factory.branches() == before
    assert not (factory.home / "factory.db").exists() or not runs(factory)


def runs(factory: Harness) -> list[str]:
    with sqlite3.connect(factory.home / "factory.db") as connection:
        return [run_id for (run_id,) in connection.execute("SELECT id FROM runs")]


def snapshot_run(factory: Harness, link: str, *flags: str) -> tuple[CliResult, list[dict[str, Any]]]:
    """Runs a Spec whose every Ticket a Gate abandons before any agent works, and returns its snapshotted Tickets.

    The Run reads the Spec as any Run does, but it starts no agent and lands nothing, so it is cheap.
    """
    factory.pipeline["start"] = "skip"
    factory.pipeline["gates"] = {
        "skip": {"kind": "command", "with": {"run": "true"}, "next": {"pass": "abandoned", "fail": "abandoned"}}
    }
    result = factory.run(link, *flags)
    assert result.code == 1, result.out + result.err
    [snapshotted] = factory.events(run_id_of(result.out), "SpecSnapshotted")
    tickets: list[dict[str, Any]] = snapshotted["snapshot"]["tickets"]
    return result, tickets


def test_a_work_items_link_on_any_gitlab_host_reads_the_spec_from_that_host(factory: Harness) -> None:
    publish(factory, "gitlab.com")

    _, tickets = snapshot_run(factory, f"https://gitlab.com/{PROJECT}/-/work_items/21")

    assert [(t["id"], t["blocked_by"]) for t in tickets] == [("28", []), ("30", ["28"])]
    assert {call[call.index("--hostname") + 1] for call in factory.glab.calls()} == {"gitlab.com"}


def test_blockers_join_front_matter_and_native_links_and_a_closed_one_anywhere_counts_as_done(factory: Harness) -> None:
    factory.glab.set(linked_items=True)
    factory.glab.issue(5, "Not a Ticket", state="closed")
    factory.glab.issue(27, "Lay the ground", state="closed")
    factory.glab.issue(5, "Elsewhere", state="closed", project="other/project")
    factory.glab.issue(28, "Greet", front(5, "'#27'", rest="Write hello.txt."), blocked_by=["other/project#5"])
    factory.glab.issue(29, "Wave", "Write wave.txt.")
    factory.glab.issue(30, "Greet again", front(29, rest="Write bye.txt."), blocked_by=[28])
    url = factory.glab.issue(21, "Greeting", kind="Issue", children=[28, 29, 30])

    _, tickets = snapshot_run(factory, url)

    assert tickets == [
        {**TICKET_FIELDS, "id": "28", "title": "Greet", "body": "Write hello.txt."},
        {**TICKET_FIELDS, "id": "29", "title": "Wave", "body": "Write wave.txt."},
        {**TICKET_FIELDS, "id": "30", "title": "Greet again", "body": "Write bye.txt.", "blocked_by": ["29", "28"]},
    ]


def test_an_open_native_blocker_in_another_project_stops_the_run(factory: Harness) -> None:
    factory.glab.set(linked_items=True)
    factory.glab.issue(6, "Elsewhere", project="other/project")
    factory.glab.issue(29, "Wave", blocked_by=["other/project#6"])
    url = factory.glab.issue(22, "Waving", kind="Issue", children=[29])
    before = factory.branches()

    result = factory.run(url)

    assert_refused(factory, result, before)
    assert result.err == "factory: #29 is blocked by open #6 of other/project, which this Run does not take\n"


def test_tickets_flag_takes_listed_child_tasks_and_part_of_tickets(factory: Harness) -> None:
    factory.glab.issue(28, "Greet")
    factory.glab.issue(29, "Wave")
    factory.glab.issue(5, "Part", part_of(1), kind="Issue")
    factory.glab.issue(6, "Also", part_of(1), kind="Issue")
    url = factory.glab.issue(1, "Map", kind="Issue", children=[28, 29])

    _, tickets = snapshot_run(factory, url, "--tickets", "28,5")

    assert [t["id"] for t in tickets] == ["28", "5"]


def test_tickets_flag_refuses_a_number_that_is_neither_a_child_task_nor_part_of_the_spec(factory: Harness) -> None:
    factory.glab.issue(28, "Greet")
    factory.glab.issue(5, "Part", part_of(1), kind="Issue")
    factory.glab.issue(6, "Other", part_of(2), kind="Issue")
    url = factory.glab.issue(1, "Map", kind="Issue", children=[28])
    before = factory.branches()

    result = factory.run(url, "--tickets", "28,5,6")

    assert_refused(factory, result, before)
    assert result.err == f"factory: #6 is not a Ticket of {url}\n"


def test_run_warns_when_base_is_not_the_default_branch_of_the_project(factory: Harness) -> None:
    url = publish(factory)
    factory.glab.set(default_branch="develop")

    result, _ = snapshot_run(factory, url)

    assert result.err == (
        f"factory: warning: base main is not the default branch develop of {PROJECT},"
        " so GitLab will not close the merged Tickets and they stay open\n"
    )


def test_check_warns_when_glab_cannot_tell_the_default_branch_of_the_project(factory: Harness) -> None:
    url = publish(factory)
    factory.glab.set(default_branch="")

    result = check(factory, url)

    assert result.code == 0, result.err
    assert result.err == (
        f"factory: warning: cannot tell the default branch of {PROJECT}: glab api gave no default branch of {PROJECT}\n"
    )


def test_part_of_tickets_are_issues_whose_leading_lines_name_the_spec_read_over_every_page_in_blocked_by_order(
    factory: Harness,
) -> None:
    factory.glab.set(search_page_size=2)
    factory.glab.issue(27, "Lay the ground", state="closed")
    factory.glab.issue(5, "Base", part_of(1, 27, rest="Write a.txt."), kind="Issue")
    factory.glab.issue(6, "Finding", "Found by #21 (map #1)\nPart of #1", kind="Issue")
    factory.glab.issue(7, "Next", part_of(1, 5, rest="Write b.txt."), kind="Issue")
    factory.glab.issue(8, "Both", "\nPart of #1\n\nBlocked by: #5, #7\n\nText.", kind="Issue")
    factory.glab.issue(9, "Later", "Intro.\n\nPart of #1", kind="Issue")
    factory.glab.issue(13, "Longer", "Part of #12\nWrite.", kind="Issue")
    factory.glab.issue(14, "Longer", "Part of #11", kind="Issue")
    factory.glab.issue(15, "Quoted", "> Part of #1\nWrite.", kind="Issue")
    url = factory.glab.issue(1, "Map", "The map.", kind="Issue")

    result, tickets = snapshot_run(factory, url)

    assert tickets == [
        {**TICKET_FIELDS, "id": "5", "title": "Base", "body": "## What to build\nWrite a.txt."},
        {**TICKET_FIELDS, "id": "7", "title": "Next", "body": "## What to build\nWrite b.txt.", "blocked_by": ["5"]},
        {**TICKET_FIELDS, "id": "8", "title": "Both", "body": "Text.", "blocked_by": ["5", "7"]},
    ]
    run_dir = factory.run_dir(run_id_of(result.out))
    assert "Part of" not in (run_dir / "tickets" / "7" / "ticket.md").read_text()


def test_child_tasks_and_part_of_tickets_mix_join_every_blocker_source_and_an_issue_in_both_counts_once(
    factory: Harness,
) -> None:
    factory.glab.set(linked_items=True)
    factory.glab.issue(2, "A", "Write.")
    factory.glab.issue(3, "B", "Write.")
    factory.glab.issue(4, "Both", part_of(1, 2))
    factory.glab.issue(5, "C", part_of(1, 2) + "\n", blocked_by=[3], kind="Issue")
    factory.glab.issue(6, "D", "Part of #1\n" + front(4), kind="Issue")
    factory.glab.issue(7, "Closed", part_of(1), state="closed", kind="Issue")
    url = factory.glab.issue(1, "Map", kind="Issue", children=[2, 3, 4])

    _, tickets = snapshot_run(factory, url)

    assert [(t["id"], t["blocked_by"]) for t in tickets] == [
        ("2", []),
        ("3", []),
        ("4", ["2"]),
        ("5", ["2", "3"]),
        ("6", ["4"]),
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


@pytest.mark.parametrize("line", ["Blocked by: #5, soon", "Blocked by:", "Blocked by: 5, #", "Blocked by: #5 #7"])
def test_a_malformed_blocked_by_line_stops_the_run_with_the_tickets_name(factory: Harness, line: str) -> None:
    factory.glab.issue(8, "Greet", f"Part of #1\n{line}\n\nWrite.", kind="Issue")
    url = factory.glab.issue(1, "Map", kind="Issue")
    before = factory.branches()

    result = factory.run(url)

    assert_refused(factory, result, before)
    assert result.err.startswith("factory: #8 Greet: Blocked by names no issue: ")


@pytest.mark.usefixtures(killed_runs_run_no_handler.__name__)
def test_finish_rebuilds_the_gitlab_tracker_from_the_specs_link(factory: Harness) -> None:
    url = publish(factory)
    factory.executor.scripts["implement"] = [APPEND]
    factory.executor.ticket_scripts[("30", "implement")] = [AgentScript(wait_for=factory.root / "never")]
    run_id = crashed_run(factory, url, lambda run_id: len(factory.events(run_id, "AttemptStarted")) == 2)
    factory.clock.sleep(STALE.seconds)
    factory.remove_remotes()

    result = factory.cli("finish", run_id)

    assert result.code == 0, result.out + result.err
    [failed] = factory.events(run_id, "LandingFailed")
    assert failed["reason"] == f"no git remote of {factory.repo} points at https://gitlab.example.com/{PROJECT}.git"
    assert "  glab mr create --repo gitlab.example.com/group/sub/project --source-branch" in result.out


FIXES = {
    "glab": "glab is not installed: install it, see https://gitlab.com/gitlab-org/cli#installation",
    "sign-in": "run `glab auth login --hostname gitlab.example.com`",
    "access": "glab cannot read group/sub/project on gitlab.example.com",
    "remote": "run `git remote add gitlab https://gitlab.example.com/group/sub/project.git`",
}


def check(factory: Harness, url: str) -> CliResult:
    return factory.cli("check", url, "--project", str(factory.project_file))


def test_check_accepts_a_glab_that_can_serve_the_spec(factory: Harness) -> None:
    url = publish(factory)

    result = check(factory, url)

    assert result.code == 0, result.err
    assert result.out.endswith(": OK\n")


@pytest.mark.parametrize("problem", ["glab", "sign-in", "access", "remote"])
def test_check_and_run_report_each_glab_problem_with_its_fix(factory: Harness, problem: str) -> None:
    url = publish(factory)
    match problem:
        case "glab":
            factory.lose_cli("glab")
        case "sign-in":
            factory.glab.set(signed_out=["gitlab.example.com"])
        case "access":
            factory.glab.set(unreadable=[PROJECT])
        case "remote":
            factory.git("remote", "remove", "gitlab")
    before = factory.branches()

    checked = check(factory, url)
    ran = factory.run(url)

    assert checked.code == 2
    assert FIXES[problem] in checked.err
    assert_refused(factory, ran, before)
    assert FIXES[problem] in ran.err


def test_check_reports_a_missing_glab_together_with_a_missing_remote(factory: Harness) -> None:
    url = publish(factory)
    factory.lose_cli("glab")
    factory.git("remote", "remove", "gitlab")

    result = check(factory, url)

    assert result.code == 2
    assert FIXES["glab"] in result.err
    assert FIXES["remote"] in result.err


def test_check_without_a_link_or_with_a_github_link_does_not_call_glab(factory: Harness) -> None:
    factory.lose_cli("glab")

    assert factory.check().code == 0
    assert check(factory, "https://github.com/Owner/target/issues/1").code == 0


def test_a_self_hosted_gitlab_whose_host_does_not_tell_stops_the_run_and_names_code_host(factory: Harness) -> None:
    factory.remove_remotes()
    url = publish(factory, "git.example.com")
    before = factory.branches()

    result = factory.run(url)

    assert_refused(factory, result, before)
    assert "name the code host with `code_host` in the project file" in result.err
