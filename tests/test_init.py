"""`factory init` writes a project file with working defaults for the repository it runs in."""

import io
from pathlib import Path

import pytest
import yaml

from factory.cli.stacks import STACKS, Stack
from harness import Harness, git


@pytest.fixture
def fresh(factory: Harness) -> Harness:
    """A repository with a remote and no project file."""
    factory.leave_project_file()
    return factory


def init(factory: Harness, *args: str) -> tuple[int, str, str]:
    result = factory.cli("init", "--project", str(factory.project_file), *args)
    return result.code, result.out, result.err


def check(factory: Harness) -> tuple[int, str]:
    result = factory.cli("check", "--project", str(factory.project_file))
    return result.code, result.err


@pytest.mark.parametrize(
    ("files", "egress", "cache"),
    [
        ({"pyproject.toml": "", "uv.lock": ""}, "pypi.org", ".venv"),
        ({"package.json": "{}"}, "registry.npmjs.org", "node_modules"),
        ({"go.mod": "module example.com/target\n"}, "proxy.golang.org", None),
    ],
    ids=["python-uv", "node", "go"],
)
def test_init_yes_writes_a_project_file_that_passes_factory_check(
    fresh: Harness, files: dict[str, str], egress: str, cache: str | None
) -> None:
    for name, text in files.items():
        (fresh.repo / name).write_text(text)

    code, out, err = init(fresh, "--yes")

    assert code == 0, err
    assert f"Wrote {fresh.project_file}" in out
    project = yaml.safe_load(fresh.project_file.read_text())
    assert project["pipeline"] == "tdd-review"
    assert project["run"] == "default"
    assert project["base"] == "main"
    assert project["sandbox"]["kind"] == "sbx"
    assert egress in project["sandbox"]["egress"]
    assert cache is None or cache in project["caches"]
    assert {"build", "test", "junit", "coverage_format", "coverage_report", "scope"} == set(project["vars"])
    assert check(fresh) == (0, "")


@pytest.mark.parametrize("stack", STACKS, ids=lambda stack: stack.name)
def test_every_stack_of_the_table_gives_a_project_file_that_passes_factory_check(fresh: Harness, stack: Stack) -> None:
    for mark in stack.marks:
        (fresh.repo / mark.replace("*", "")).write_text("")

    assert init(fresh, "--yes")[0] == 0

    assert check(fresh) == (0, "")


def test_every_value_says_where_it_came_from(fresh: Harness) -> None:
    (fresh.repo / "go.mod").write_text("module example.com/target\n")

    init(fresh, "--yes")

    text = fresh.project_file.read_text()
    for line in text.splitlines():
        if line.strip() and not line.startswith("#") and not line.endswith(":"):
            assert "  # " in line, line
    assert "base: main  # the default branch of the remote origin" in text
    assert "the stack Go, from go.mod" in text
    assert "follow from the git remote origin, git@github.com:owner/target.git" in text


def test_base_is_the_default_branch_of_the_remote(fresh: Harness) -> None:
    git(fresh.repo, "push", "-q", "origin", "main:develop")
    git(fresh.remote, "symbolic-ref", "HEAD", "refs/heads/develop")
    (fresh.repo / "go.mod").write_text("")

    init(fresh, "--yes")

    assert yaml.safe_load(fresh.project_file.read_text())["base"] == "develop"


def test_a_value_it_could_not_detect_is_a_commented_placeholder_that_factory_check_names(fresh: Harness) -> None:
    code, out, _ = init(fresh, "--yes")

    assert code == 0
    assert "vars.build, vars.test" in out
    text = fresh.project_file.read_text()
    assert "  # build: <the command that builds the project>  # not detected" in text
    assert "# egress: [<the host of your package registry>]" in text

    code, err = check(fresh)

    assert code == 2
    assert "unknown variable build" in err
    assert "unknown variable test" in err


def test_a_repository_without_a_remote_leaves_base_as_a_placeholder(fresh: Harness) -> None:
    fresh.remove_remotes()

    init(fresh, "--yes")

    assert "# base: <the branch a Run starts from>  # not detected" in fresh.project_file.read_text()
    assert "missing base" in check(fresh)[1]


def test_a_directory_outside_git_leaves_base_as_a_placeholder(factory: Harness, tmp_path: Path) -> None:
    result = factory.cli("init", "--yes", "--project", str(tmp_path / "factory.yaml"))

    assert result.code == 0, result.err
    assert "because git failed" in (tmp_path / "factory.yaml").read_text()


def test_a_remote_that_names_no_default_branch_leaves_base_as_a_placeholder(fresh: Harness) -> None:
    git(fresh.remote, "symbolic-ref", "HEAD", "refs/heads/gone")

    init(fresh, "--yes")

    assert "the remote origin names no default branch" in fresh.project_file.read_text()


def test_a_remote_it_cannot_reach_leaves_base_as_a_placeholder(fresh: Harness) -> None:
    git(fresh.repo, "remote", "set-url", "origin", "git@github.com:owner/other.git")
    git(fresh.repo, "config", "--unset-all", f"url.{fresh.remote}.insteadOf")
    git(fresh.repo, "config", "--add", f"url.{fresh.root / 'gone.git'}.insteadOf", "git@github.com:owner/other.git")

    init(fresh, "--yes")

    assert "# base: <the branch a Run starts from>  # not detected, because `git ls-remote origin` failed" in (
        fresh.project_file.read_text()
    )


def test_a_remote_no_kind_serves_is_a_commented_code_host_that_factory_check_names(fresh: Harness) -> None:
    (fresh.repo / "go.mod").write_text("")
    for name in git(fresh.repo, "remote").split():
        git(fresh.repo, "remote", "remove", name)
    git(fresh.repo, "remote", "add", "origin", str(fresh.remote))

    init(fresh, "--yes")

    assert "# code_host: <one of github, gitlab>  # not detected" in fresh.project_file.read_text()
    assert "name the code host with `code_host`" in check(fresh)[1]


def test_init_leaves_an_existing_project_file_as_it_is(fresh: Harness) -> None:
    fresh.project_file.write_text("mine: true\n")

    code, _, err = init(fresh, "--yes")

    assert code == 2
    assert "stays as it is" in err
    assert fresh.project_file.read_text() == "mine: true\n"
    assert not fresh.project_file.with_name("factory.yaml.new").exists()


def test_init_force_writes_a_new_file_next_to_an_existing_one(fresh: Harness) -> None:
    fresh.project_file.write_text("mine: true\n")
    (fresh.repo / "go.mod").write_text("")

    code, out, _ = init(fresh, "--yes", "--force")

    new = fresh.project_file.with_name("factory.yaml.new")
    assert code == 0
    assert f"Wrote {new}" in out
    assert fresh.project_file.read_text() == "mine: true\n"
    assert yaml.safe_load(new.read_text())["pipeline"] == "tdd-review"


def test_init_without_yes_asks_and_keeps_what_the_owner_leaves_empty(fresh: Harness) -> None:
    (fresh.repo / "go.mod").write_text("")
    fresh.stdin = io.StringIO("trunk\n\nmake test JUNIT=$GATE_OUT/junit.xml\n")

    code, out, _ = init(fresh)

    assert code == 0
    assert "base [main]: " in out
    project = yaml.safe_load(fresh.project_file.read_text())
    assert project["base"] == "trunk"
    assert project["vars"]["build"] == "go build ./..."
    assert project["vars"]["test"] == "make test JUNIT=$GATE_OUT/junit.xml"
    assert "base: trunk  # given by the owner" in fresh.project_file.read_text()
