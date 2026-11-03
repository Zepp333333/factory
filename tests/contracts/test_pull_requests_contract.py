"""The contract of the PullRequests port, which every PullRequests kind of `wiring` passes.

The harness registers no PullRequests of its own: the real kinds talk to the fake `gh` and `glab`.
Each kind has a Bench in `FAKED`, which names the git remote the kind serves and sets the CI of its fake.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from factory.ports.tracker import PullRequests
from factory.wiring import REGISTRIES
from harness import REPO, Harness
from hosts import Host, fake_of, host, kinds  # noqa: F401

LOG = "FAILED tests/test_clock.py::test_run_lasts_0s - assert 1 == 0"
OLDER = "0" * 40


@dataclass
class Bench:
    """A Bench gives the git remote of the repository a kind's PR opens in, and sets the CI of the PR's head."""

    factory: Harness
    remote: str
    other: str

    def __post_init__(self) -> None:
        """Gets the fake ready to serve the remote."""

    def body(self, pr: str) -> str:
        raise NotImplementedError

    def ci(self, state: str) -> None:
        """Sets the CI of the PR's head commit to `state`, with the job `unit` failed for `failed`."""
        raise NotImplementedError


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


FAKED: dict[str, Any] = {
    "github": lambda factory: GitHub(factory, f"git@github.com:{REPO}.git", "git@github.com:owner/other.git"),
    "gitlab": lambda factory: GitLab(
        factory, "git@gitlab.example.com:group/sub/project.git", "git@gitlab.example.com:group/other.git"
    ),
}


@dataclass
class Opened:
    bench: Bench
    pull_requests: PullRequests
    pr: str
    head: str


@pytest.fixture(params=kinds(REGISTRIES.pull_requests), ids=str)
def opened(request: pytest.FixtureRequest, host: Host, tmp_path: Path) -> Opened:  # noqa: F811
    """Pushes the branch `feature` and opens its PR into `main` with the kind that serves the Bench's remote."""
    name = request.param.name
    bench: Bench = fake_of(request.param, "pull_requests", FAKED)(host.factory)
    host.factory.git("push", "-q", "origin", "feature")
    pull_requests = REGISTRIES.pull_requests[name].make(bench.remote, host.tools)
    body = tmp_path / "body.md"
    body.write_text("Lands Spec Greeting.\n")
    pr = pull_requests.open("feature", "main", "Greeting", body)
    return Opened(bench, pull_requests, pr, host.factory.git("rev-parse", "feature"))


def test_the_kind_accepts_the_remote_it_serves_and_no_other(opened: Opened) -> None:
    assert opened.pull_requests.accepts(opened.bench.remote)
    assert opened.pull_requests.accepts(opened.pull_requests.url())
    assert not opened.pull_requests.accepts(opened.bench.other)


def test_open_opens_the_pr_with_its_body_and_edit_replaces_the_body(opened: Opened, tmp_path: Path) -> None:
    assert opened.bench.body(opened.pr) == "Lands Spec Greeting.\n"
    edited = tmp_path / "edited.md"
    edited.write_text("Lands Spec Greeting, edited.\n")

    opened.pull_requests.edit(opened.pr, edited)

    assert opened.bench.body(opened.pr) == "Lands Spec Greeting, edited.\n"
    assert opened.pull_requests.open_command("feature", "main", "Greeting", edited)


def test_head_is_the_commit_the_prs_branch_is_at(opened: Opened) -> None:
    assert opened.pull_requests.head(opened.pr) == opened.head


@pytest.mark.parametrize("state", ["none", "pending", "passed", "failed"])
def test_ci_of_the_head_tells_its_state_and_a_failed_one_its_jobs_with_their_logs(opened: Opened, state: str) -> None:
    opened.bench.ci(state)

    status = opened.pull_requests.ci(opened.pr, opened.head)

    assert status.state == state
    assert [(job.name, LOG in job.log) for job in status.jobs] == ([("unit", True)] if state == "failed" else [])


def test_ci_of_a_commit_other_than_the_head_is_pending(opened: Opened) -> None:
    opened.bench.ci("passed")

    assert opened.pull_requests.ci(opened.pr, OLDER).state == "pending"
