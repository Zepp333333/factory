"""`factory doctor` checks what a Run needs on the machine and in the repository, and names the fix of each failure."""

from pathlib import Path
from typing import Any

import pytest

from harness import Harness, git
from test_factory_layer import single_review_pipeline


def doctor(factory: Harness, *args: str) -> Any:
    return factory.cli("doctor", "--project", str(factory.project_file), *args)


def on_sbx_with_labels(factory: Harness) -> None:
    """Has the project run on `sbx` with a Run graph that selects by the label `ready-for-agent`."""
    factory.project.update(run="approve-then-merge", sandbox={"kind": "sbx"})
    single_review_pipeline(factory)


def failed(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("FAIL")]


def test_doctor_names_the_fix_of_each_check_on_a_machine_without_sbx_and_a_tracker_without_the_ready_label(
    factory: Harness,
) -> None:
    on_sbx_with_labels(factory)
    factory.sbx.command = ("/nonexistent/sbx",)
    factory.gh.write({**factory.gh.state(), "repo_labels": {"owner/target": ["in-progress"]}})

    result = doctor(factory)

    assert result.code == 1
    out = result.out
    assert "FAIL  sbx is installed: cannot run sbx" in out
    assert "      fix: brew trust docker/tap && brew install docker/tap/sbx" in out
    assert "FAIL  sbx is signed in" in out
    assert "      fix: sbx login" in out
    assert "FAIL  Sandboxes reach nothing by default" in out
    assert "      fix: sbx policy init deny-all" in out
    assert "FAIL  the subscription of anthropic is in Sandboxes" in out
    assert "      fix: sbx run --name factory-login claude" in out
    assert "FAIL  label ready-for-agent on owner/target" in out
    assert "      fix: gh label create ready-for-agent --repo owner/target" in out
    # One failure hides no other check, so what holds is still reported.
    assert "ok    label in-progress on owner/target" in out
    assert "ok    access to owner/target" in out
    assert "ok    the project file" in out


def test_doctor_passes_on_a_machine_that_has_everything(factory: Harness) -> None:
    on_sbx_with_labels(factory)
    factory.gh.write({**factory.gh.state(), "repo_labels": {"owner/target": ["ready-for-agent", "in-progress", "bug"]}})

    result = doctor(factory)

    assert result.code == 0, result.out
    assert failed(result.out) == []
    assert "ok    sbx is installed" in result.out
    assert result.out.endswith("Every check passed.\n")


@pytest.mark.parametrize(
    ("state", "holds", "fails"),
    [
        (
            {"allow_all": True, "oauth": False},
            "sbx is signed in",
            ("Sandboxes reach nothing by default", "the subscription of anthropic is in Sandboxes"),
        ),
        ({"signed_out": True}, "sbx is installed", ("sbx is signed in: sbx secret failed",)),
    ],
    ids=["every-host-and-no-subscription", "signed-out"],
)
def test_doctor_tells_what_sbx_lacks(
    factory: Harness, state: dict[str, bool], holds: str, fails: tuple[str, ...]
) -> None:
    on_sbx_with_labels(factory)
    factory.sbx.set(**state)

    out = doctor(factory).out

    assert f"ok    {holds}" in out
    for check in fails:
        assert f"FAIL  {check}" in out


@pytest.mark.parametrize(
    ("tracker", "project", "denied", "create_label", "sign_in"),
    [
        (
            "github",
            "owner/target",
            {"denied": True},
            "gh label create ready-for-agent --repo owner/target",
            "gh auth login",
        ),
        (
            "gitlab",
            "group/sub/project",
            {"signed_out": ["gitlab.example.com"], "unreadable": ["group/sub/project"]},
            "glab label create --name ready-for-agent --repo https://gitlab.example.com/group/sub/project",
            "glab auth login --hostname gitlab.example.com",
        ),
    ],
    ids=["github", "gitlab"],
)
def test_doctor_reads_access_and_labels_through_the_cli_of_the_code_host_and_goes_on_without_access(
    factory: Harness, tracker: str, project: str, denied: dict[str, Any], create_label: str, sign_in: str
) -> None:
    fake: Any = factory.gh
    if tracker == "gitlab":
        fake = factory.glab
        git(factory.repo, "remote", "remove", "origin")
        fake.issue(1, "Spec", kind="Issue")
    on_sbx_with_labels(factory)
    fake.write({**fake.state(), "repo_labels": {project: ["in-progress"]}})

    out = doctor(factory).out

    assert f"ok    access to {project}" in out
    assert f"ok    label in-progress on {project}" in out
    assert f"FAIL  label ready-for-agent on {project}" in out
    assert f"      fix: {create_label}" in out

    fake.write({**fake.state(), **denied})

    result = doctor(factory)

    assert result.code == 1
    assert f"FAIL  access to {project}" in result.out
    assert f"      fix: {sign_in}" in result.out
    assert f"FAIL  labels of {project}" in result.out
    assert "ok    sbx is installed" in result.out


def test_doctor_without_a_project_file_checks_the_machine_and_says_to_run_init(factory: Harness) -> None:
    missing = factory.repo / "other.yaml"

    result = factory.cli("doctor", "--project", str(missing))

    assert result.code == 1
    assert "ok    sbx is installed" in result.out
    assert "FAIL  the project file" in result.out
    assert "      fix: factory init" in result.out


def test_doctor_outside_a_git_repository_tells_that_and_still_checks_the_rest(factory: Harness, tmp_path: Path) -> None:
    result = factory.cli("doctor", "--project", str(tmp_path / "factory.yaml"))

    assert result.code == 1
    assert "FAIL  the repository" in result.out
    assert "      fix: run `factory doctor` in a git repository" in result.out
    assert "uv is installed" in result.out
    assert "ok    sbx is installed" in result.out


def test_doctor_names_what_factory_check_finds_in_the_project_file(factory: Harness) -> None:
    factory.project["max_parallel"] = "many"

    result = doctor(factory)

    assert result.code == 1
    assert "FAIL  the project file: " in result.out
    assert "max_parallel" in result.out
    assert "      fix: change what `factory check` names" in result.out


def test_doctor_names_the_fix_of_a_missing_uv(factory: Harness) -> None:
    (factory.bin / "uv").touch()
    factory.lose_cli("uv")

    out = doctor(factory).out

    assert "FAIL  uv is installed" in out
    assert "      fix: brew install uv" in out


def test_doctor_tells_a_remote_no_kind_serves(factory: Harness) -> None:
    for name in git(factory.repo, "remote").split():
        git(factory.repo, "remote", "remove", name)
    git(factory.repo, "remote", "add", "origin", "https://example.com/owner/target.git")

    out = doctor(factory).out

    assert "FAIL  a code host serves the git remotes" in out
    assert "      fix: name the code host with `code_host` in the project file" in out


def test_doctor_accepts_a_repository_without_a_remote(factory: Harness) -> None:
    factory.remove_remotes()

    result = doctor(factory)

    assert result.code == 0, result.out
    assert "ok    no git remote, so a Run keeps its PR in the folder of its Spec" in result.out


def gates_of(factory: Harness, **commands: str) -> None:
    """Adds a Gate that runs each of `commands`, in their order, before `catch_up`."""
    names = list(commands)
    factory.pipeline["gates"] = {
        name: {
            "kind": "command",
            "with": {"run": command},
            "next": {"pass": names[index + 1] if index + 1 < len(names) else "catch_up", "fail": "abandoned"},
        }
        for index, (name, command) in enumerate(commands.items())
    }
    factory.pipeline["stages"]["implement"]["next"] = {"done": names[0]}


def publish(factory: Harness, files: dict[str, str]) -> None:
    """Commits `files` on the base branch and pushes them to its remote."""
    for name, text in files.items():
        (factory.repo / name).write_text(text)
    git(factory.repo, "add", *files)
    git(factory.repo, "commit", "-q", "-m", "files")
    git(factory.repo, "push", "-q", "origin", "main")


def test_doctor_gates_fails_on_a_repository_whose_tests_are_red_on_the_base_branch(factory: Harness) -> None:
    publish(factory, {"tests.sh": "echo 'test_export failed'\nexit 3\n", "build.sh": "exit 0\n"})
    gates_of(factory, build="sh build.sh", tests="sh tests.sh")

    result = doctor(factory, "--gates")

    assert result.code == 1
    assert "ok    Gate build on main" in result.out
    assert "FAIL  Gate tests on main: exit code 3" in result.out
    assert "test_export failed" in result.out
    assert "      fix: make the Gate pass on main before a Run" in result.out


def test_doctor_gates_passes_when_the_gates_are_green_and_leaves_nothing_behind(factory: Harness) -> None:
    publish(factory, {"tests.sh": "exit 0\n"})
    gates_of(factory, tests="sh tests.sh")
    worktrees = git(factory.repo, "worktree", "list")
    branches = git(factory.repo, "branch", "--list")

    result = doctor(factory, "--gates")

    assert result.code == 0, result.out
    assert "ok    Gate tests on main" in result.out
    assert git(factory.repo, "worktree", "list") == worktrees
    assert git(factory.repo, "branch", "--list") == branches


def test_doctor_gates_runs_what_the_remote_holds_and_not_the_working_tree(factory: Harness) -> None:
    publish(factory, {"tests.sh": "exit 1\n"})
    (factory.repo / "tests.sh").write_text("exit 0\n")
    gates_of(factory, tests="sh tests.sh")

    assert doctor(factory, "--gates").code == 1


def test_doctor_gates_runs_a_gate_twice_in_the_graph_only_once_and_judges_a_gate_with_from(factory: Harness) -> None:
    publish(factory, {"tests.sh": 'cp junit.xml cover.info "$1"\n'} | fixture_reports())
    factory.pipeline["gates"] = {
        "tests": {
            "kind": "tests",
            "with": {"run": 'sh tests.sh "$GATE_OUT"; echo ran >> "$COUNT"', "junit": "junit.xml"},
            "next": {"pass": "coverage", "fail": "abandoned"},
        },
        "coverage": {
            "kind": "coverage",
            "from": "tests",
            "with": {"format": "lcov", "report": "cover.info", "min_total": 100},
            "next": {"pass": "tests_again", "fail": "abandoned"},
        },
        "tests_again": {
            "kind": "tests",
            "with": {"run": 'sh tests.sh "$GATE_OUT"; echo ran >> "$COUNT"', "junit": "junit.xml"},
            "next": {"pass": "coverage_again", "fail": "abandoned"},
        },
        "coverage_again": {
            "kind": "coverage",
            "from": "tests_again",
            "with": {"format": "lcov", "report": "cover.info", "min_total": 100},
            "next": {"pass": "catch_up", "fail": "abandoned"},
        },
    }
    factory.pipeline["stages"]["implement"]["next"] = {"done": "tests"}
    count = factory.root / "count"

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("COUNT", str(count))
        result = doctor(factory, "--gates")

    assert result.code == 1, result.out
    assert "ok    Gate tests on main" in result.out
    assert "FAIL  Gate coverage on main: 50.0% of 2 lines covered in total, below 100%" in result.out
    assert "tests_again" not in result.out and "coverage_again" not in result.out
    assert count.read_text() == "ran\n"


def fixture_reports() -> dict[str, str]:
    junit = '<testsuite><testcase classname="a" name="b"/></testsuite>\n'
    return {"junit.xml": junit, "cover.info": "SF:src/app.py\nDA:1,1\nDA:2,0\nend_of_record\n"}


def test_doctor_gates_tells_a_sandbox_that_does_not_start(factory: Harness) -> None:
    gates_of(factory, tests="exit 0")
    factory.project["sandbox"] = {"kind": "sbx"}
    factory.sbx.command = ("/nonexistent/sbx",)

    result = doctor(factory, "--gates")

    assert result.code == 1
    assert "FAIL  the Gates on main: cannot run sbx" in result.out


def test_doctor_gates_stops_a_gate_at_its_timeout(factory: Harness) -> None:
    factory.pipeline["gates"] = {
        "slow": {
            "kind": "command",
            "with": {"run": "sleep 20", "timeout": "1s"},
            "next": {"pass": "catch_up", "fail": "abandoned"},
        }
    }
    factory.pipeline["stages"]["implement"]["next"] = {"done": "slow"}

    result = doctor(factory, "--gates")

    assert result.code == 1
    assert "FAIL  Gate slow on main: timed out after 1s" in result.out


def test_doctor_gates_without_a_valid_project_file_says_it_ran_no_gate(factory: Harness) -> None:
    factory.project["max_parallel"] = "many"

    result = doctor(factory, "--gates")

    assert result.code == 1
    assert "FAIL  the Gates: the project file does not load" in result.out


def test_doctor_gates_tells_a_base_branch_it_cannot_fetch(factory: Harness) -> None:
    factory.project["base"] = "nowhere"

    result = doctor(factory, "--gates")

    assert result.code == 1
    assert "FAIL  the Gates on nowhere: " in result.out
