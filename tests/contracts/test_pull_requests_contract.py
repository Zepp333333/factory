"""The contract of the PullRequests port, which every PullRequests kind of `wiring` and a fake kind of `tests/` pass.

The harness registers no PullRequests in the place of a real kind: the real kinds talk to the fake `gh` and `glab`,
and the `CodeHost` of `fake_kinds` is a kind only the tests know.
Each kind has a Bench in `FAKED`, which names the git remote the kind serves and sets the CI of its fake.
A kind on a code host pushes the PR's branch and reads its head and CI there, and the folder of a Spec pushes nothing,
runs no CI and serves no git remote.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from factory.ports.kind import Kind
from factory.ports.tracker import PullRequests, TrackerError
from factory.wiring import REGISTRIES
from fake_kinds import CodeHost
from harness import REPO, Harness
from hosts import Host, Picked, fake_of, host, kinds  # noqa: F401

LOG = "FAILED tests/test_clock.py::test_run_lasts_0s - assert 1 == 0"
OLDER = "0" * 40


@dataclass
class Bench:
    """A Bench gives the git remote of the repository a kind's PR opens in, and sets the CI of the PR's head."""

    factory: Harness
    remote: str
    other: str
    config: str = ""

    @property
    def hosted(self) -> bool:
        """Tells whether the kind lands on a code host, which the PR's branch is pushed to and which runs CI."""
        return True

    def __post_init__(self) -> None:
        """Gets the fake ready to serve the remote."""

    def body(self, pr: str) -> str:
        raise NotImplementedError

    def ci(self, state: str) -> None:
        """Sets the CI of the PR's head commit to `state`, with the job `unit` failed for `failed`."""
        raise NotImplementedError

    def kind(self, name: str) -> Kind[Any]:
        """Returns the kind the Bench serves, which is the one `wiring` registers under `name`."""
        return REGISTRIES.pull_requests[name]


class Folder(Bench):
    @property
    def hosted(self) -> bool:
        return False

    def body(self, pr: str) -> str:
        return Path(pr).read_text().split("\n\n", 2)[2]

    def ci(self, state: str) -> None:
        """Sets nothing, because a folder runs no CI."""


class GitHub(Bench):
    def body(self, pr: str) -> str:
        return str(self.factory.gh.pulls()[int(pr.rsplit("/", 1)[1]) - 1]["body"])

    def ci(self, state: str) -> None:
        unit = "https://github.com/Owner/target/actions/runs/5/job/91"
        buckets = {"pending": "pending", "passed": "pass", "failed": "fail"}
        if state in buckets:
            self.factory.gh.checks([{"name": "unit", "bucket": buckets[state], "link": unit}])
        self.factory.gh.log(91, LOG)


class GitLab(Bench):
    def __post_init__(self) -> None:
        self.factory.glab.set(hosts={"gitlab.example.com": {"projects": {"group/sub/project": {"items": {}}}}})

    def body(self, pr: str) -> str:
        return str(self.factory.glab.merge_requests()[int(pr.rsplit("/", 1)[1]) - 1]["body"])

    def ci(self, state: str) -> None:
        job = {"id": 11, "name": "unit", "web_url": "https://gitlab.example.com/-/jobs/11", "trace": LOG}
        statuses = {"pending": "running", "passed": "success", "failed": "failed"}
        if state in statuses:
            self.factory.glab.pipelines({"status": statuses[state], "jobs": [job] if state == "failed" else []})


class Hosted(Bench):
    def __post_init__(self) -> None:
        self.code_host = CodeHost(Path(self.remote))

    def body(self, pr: str) -> str:
        return self.code_host.pull(pr).body

    def ci(self, state: str) -> None:
        self.code_host.ci_state = state
        self.code_host.failed_log = LOG

    def kind(self, name: str) -> Kind[Any]:
        return self.code_host.kind()


FAKED: dict[str, Any] = {
    "github": lambda factory: GitHub(
        factory, f"git@github.com:{REPO}.git", "git@github.com:owner/other.git", ".github/workflows/ci.yml"
    ),
    "gitlab": lambda factory: GitLab(
        factory,
        "git@gitlab.example.com:group/sub/project.git",
        "git@gitlab.example.com:group/other.git",
        ".gitlab-ci.yml",
    ),
    "folder": lambda factory: Folder(factory, str(factory.root / "pull_requests"), f"git@github.com:{REPO}.git"),
}

# The kinds only the tests know, which `wiring` does not register.
TEST_KINDS: dict[str, Any] = {
    "hosted": lambda factory: Hosted(factory, str(factory.remote), f"git@github.com:{REPO}.git", ".host-ci/ci.yml"),
}


@dataclass
class Opened:
    bench: Bench
    pull_requests: PullRequests
    pr: str
    head: str


@pytest.fixture(params=[*kinds(REGISTRIES.pull_requests), *(Picked("tests", name) for name in TEST_KINDS)], ids=str)
def opened(request: pytest.FixtureRequest, host: Host, tmp_path: Path) -> Opened:  # noqa: F811
    """Pushes the branch `feature` and opens its PR into `main` with the kind that serves the Bench's remote."""
    name = request.param.name
    bench: Bench = fake_of(request.param, "pull_requests", {**FAKED, **TEST_KINDS})(host.factory)
    host.factory.git("push", "-q", "origin", "feature")
    pull_requests = bench.kind(name).make(bench.remote, host.tools)
    body = tmp_path / "body.md"
    body.write_text("Lands Spec Greeting.\n")
    pr = pull_requests.open("feature", "main", "Greeting", body)
    return Opened(bench, pull_requests, pr, host.factory.git("rev-parse", "feature"))


def test_the_kind_accepts_the_remote_it_serves_and_no_other(opened: Opened) -> None:
    assert opened.pull_requests.accepts(opened.bench.remote) == opened.bench.hosted
    assert opened.pull_requests.accepts(opened.pull_requests.url()) == opened.bench.hosted
    assert not opened.pull_requests.accepts(opened.bench.other)


def test_a_kind_pushes_and_guards_its_ci_config_only_on_a_code_host(opened: Opened) -> None:
    assert opened.pull_requests.pushes == opened.bench.hosted
    paths = opened.pull_requests.ci_config_paths
    assert opened.bench.config.startswith(paths) if opened.bench.hosted else paths == ()


def test_open_opens_the_pr_with_its_body_and_edit_replaces_the_body(opened: Opened, tmp_path: Path) -> None:
    assert opened.bench.body(opened.pr) == "Lands Spec Greeting.\n"
    edited = tmp_path / "edited.md"
    edited.write_text("Lands Spec Greeting, edited.\n")

    opened.pull_requests.edit(opened.pr, edited)

    assert opened.bench.body(opened.pr) == "Lands Spec Greeting, edited.\n"
    assert bool(opened.pull_requests.open_command("feature", "main", "Greeting", edited)) == opened.bench.hosted


def test_head_is_the_commit_the_prs_branch_is_at_on_a_code_host(opened: Opened) -> None:
    if not opened.bench.hosted:
        with pytest.raises(TrackerError):
            opened.pull_requests.head(opened.pr)
        return
    assert opened.pull_requests.head(opened.pr) == opened.head


@pytest.mark.parametrize("state", ["none", "pending", "passed", "failed"])
def test_ci_of_the_head_tells_its_state_and_a_failed_one_its_jobs_with_their_logs(opened: Opened, state: str) -> None:
    opened.bench.ci(state)

    status = opened.pull_requests.ci(opened.pr, opened.head)

    assert status.state == (state if opened.bench.hosted else "absent")
    failed = state == "failed" and opened.bench.hosted
    assert [(job.name, LOG in job.log) for job in status.jobs] == ([("unit", True)] if failed else [])


def test_ci_of_a_commit_other_than_the_head_is_pending(opened: Opened) -> None:
    opened.bench.ci("passed")

    assert opened.pull_requests.ci(opened.pr, OLDER).state == ("pending" if opened.bench.hosted else "absent")


def test_a_layer_of_a_stack_lands_with_its_number(opened: Opened, tmp_path: Path) -> None:
    opened.bench.factory.git("push", "-q", "origin", "feature:feature-1")
    body = tmp_path / "layer.md"
    body.write_text("Lands the first layer.\n")

    pr = opened.pull_requests.open("feature-1", "main", "One", body, 1)

    assert opened.bench.body(pr) == "Lands the first layer.\n"
    if not opened.bench.hosted:
        assert Path(pr).name == "01-feature-1.md"
