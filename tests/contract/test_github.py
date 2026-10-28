"""Seam 3 contract tests check that real GitHub behaves as GitHubTracker and the fake `gh` assume.

They run only with FACTORY_CONTRACT=1 and FACTORY_GITHUB_REPO=<owner>/<name>, a throwaway repository
the owner's `gh` can write.
The module publishes a Spec with sub-issues and blocked-by links there, puts a label and a comment on one of them
and closes every issue at the end,
and the PR test opens a PR from a branch of its own and then closes the PR and deletes the branch.
"""

import base64
import json
import os
import secrets
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from factory.github import GitHubTracker
from factory.tracker import SpecSnapshot, TicketSnapshot, TrackerError

REPO = os.environ.get("FACTORY_GITHUB_REPO", "")

pytestmark = pytest.mark.skipif(
    os.environ.get("FACTORY_CONTRACT") != "1" or not REPO, reason="set FACTORY_CONTRACT=1 and FACTORY_GITHUB_REPO"
)


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def api(method: str, path: str, *fields: str) -> dict[str, object]:
    flags = [arg for field in fields for arg in ("-F" if field.split("=", 1)[1].isdigit() else "-f", field)]
    out = gh("api", "-X", method, path, *flags)
    data: dict[str, object] = json.loads(out) if out.strip() else {}
    return data


def issue(title: str, body: str) -> tuple[int, int]:
    """Opens an issue and returns its number and its id, which the sub-issue and dependency APIs take."""
    created = api("POST", f"repos/{REPO}/issues", f"title={title}", f"body={body}")
    return int(str(created["number"])), int(str(created["id"]))


@dataclass
class Published:
    link: str
    spec: int
    first: int
    second: int
    done: int


@pytest.fixture(scope="module")
def published() -> Iterator[Published]:
    tag = secrets.token_hex(3)
    spec, _ = issue(f"Contract Spec {tag}", "Say hello.")
    done, done_id = issue(f"Contract done {tag}", "Already done.")
    first, first_id = issue(f"Contract first {tag}", "Write hello.txt.")
    second, second_id = issue(f"Contract second {tag}", "Write bye.txt.")
    numbers = [spec, done, first, second]
    try:
        for child in (done_id, first_id, second_id):
            api("POST", f"repos/{REPO}/issues/{spec}/sub_issues", f"sub_issue_id={child}")
        api("POST", f"repos/{REPO}/issues/{first}/dependencies/blocked_by", f"issue_id={done_id}")
        api("POST", f"repos/{REPO}/issues/{second}/dependencies/blocked_by", f"issue_id={first_id}")
        api("PATCH", f"repos/{REPO}/issues/{done}", "state=closed")
        yield Published(f"https://github.com/{REPO}/issues/{spec}", spec, first, second, done)
    finally:
        for number in numbers:
            api("PATCH", f"repos/{REPO}/issues/{number}", "state=closed")


def test_spec_snapshot_holds_open_sub_issues_with_open_blockers(published: Published) -> None:
    snapshot = GitHubTracker(published.link).read_spec()

    tag = snapshot.title.rsplit(" ", 1)[1]
    assert snapshot == SpecSnapshot(
        ref=published.link,
        title=f"Contract Spec {tag}",
        body="Say hello.",
        tickets=[
            TicketSnapshot(str(published.first), f"Contract first {tag}", "Write hello.txt.", []),
            TicketSnapshot(str(published.second), f"Contract second {tag}", "Write bye.txt.", [str(published.first)]),
        ],
    )


def test_open_blocker_outside_the_selection_stops_the_read(published: Published) -> None:
    with pytest.raises(TrackerError, match=f"#{published.second} is blocked by open #{published.first}"):
        GitHubTracker(published.link, [published.second]).read_spec()


def test_number_outside_the_sub_issues_stops_the_read(published: Published) -> None:
    with pytest.raises(TrackerError, match=f"#{published.spec} is not a sub-issue"):
        GitHubTracker(published.link, [published.spec]).read_spec()


def test_pull_request_opens_from_a_pushed_branch_with_the_body_file(published: Published, tmp_path: Path) -> None:
    base = gh("api", f"repos/{REPO}", "--jq", ".default_branch").strip()
    sha = gh("api", f"repos/{REPO}/git/ref/heads/{base}", "--jq", ".object.sha").strip()
    head = f"factory/contract-{secrets.token_hex(3)}"
    api("POST", f"repos/{REPO}/git/refs", f"ref=refs/heads/{head}", f"sha={sha}")
    try:
        content = base64.b64encode(b"hello\n").decode()
        api("PUT", f"repos/{REPO}/contents/{head}.txt", f"message=Add {head}", f"content={content}", f"branch={head}")
        body = tmp_path / "pull_request.md"
        body.write_text(f"Closes #{published.first}\n")

        url = GitHubTracker(published.link).open(head, base, "Contract PR", body)

        number = url.rsplit("/", 1)[1]
        try:
            pull = api("GET", f"repos/{REPO}/pulls/{number}")
            assert pull["title"] == "Contract PR"
            assert str(pull["body"]).replace("\r\n", "\n").strip() == f"Closes #{published.first}"
            assert url == pull["html_url"]
        finally:
            api("PATCH", f"repos/{REPO}/pulls/{number}", "state=closed")
    finally:
        gh("api", "-X", "DELETE", f"repos/{REPO}/git/refs/heads/{head}")


def test_projection_labels_comments_and_unlabels_an_issue_and_leaves_it_open(published: Published) -> None:
    tracker = GitHubTracker(published.link)
    number = published.first

    tracker.add_label(str(number), "factory:needs-human")
    tracker.comment(str(number), 1, "Decision judge waits.")

    labels = json.loads(gh("api", f"repos/{REPO}/issues/{number}/labels"))
    assert [label["name"] for label in labels] == ["factory:needs-human"]
    comments = json.loads(gh("api", f"repos/{REPO}/issues/{number}/comments"))
    assert [comment["body"] for comment in comments] == ["Decision judge waits."]

    tracker.remove_label(str(number), "factory:needs-human")
    tracker.remove_label(str(number), "factory:needs-human")

    assert json.loads(gh("api", f"repos/{REPO}/issues/{number}/labels")) == []
    assert api("GET", f"repos/{REPO}/issues/{number}")["state"] == "open"
