"""Seam 3 contract tests check that a real GitLab answers as GitLabTracker and the fake `glab` assume.

They run only with FACTORY_CONTRACT=1 and FACTORY_GITLAB_SPEC=<link>, the issue of a Spec the owner published
on a self-managed project the owner's `glab` can read: open child Tasks, one of which blocks another
by `blocked_by` in the front matter of its description.
A second Spec, FACTORY_GITLAB_PART_OF=<link>, is published with `Part of #N` lines instead of child Tasks.
Reading changes nothing, except the projection test and the last test.
The projection test also needs FACTORY_GITLAB_TICKET=<iid>, a Task of that project it may label and note,
and it deletes the note and the label it adds.
The landing test lands an MR and needs FACTORY_GITLAB_LAND=1 as well.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from factory.gitlab import GitLabPullRequests, GitLabTracker
from factory.states import TicketState
from factory.tracker import TrackerError

LINK = os.environ.get("FACTORY_GITLAB_SPEC", "")

pytestmark = pytest.mark.skipif(
    os.environ.get("FACTORY_CONTRACT") != "1" or not LINK, reason="set FACTORY_CONTRACT=1 and FACTORY_GITLAB_SPEC"
)


def test_spec_snapshot_holds_the_open_child_tasks_with_their_blockers_and_no_front_matter() -> None:
    snapshot = GitLabTracker(LINK).read_spec()

    assert snapshot.ref == LINK
    assert snapshot.title
    assert snapshot.tickets
    ids = {ticket.id for ticket in snapshot.tickets}
    for ticket in snapshot.tickets:
        assert re.fullmatch(r"\d+", ticket.id)
        assert not ticket.body.startswith("---")
        assert set(ticket.blocked_by) <= ids
    assert any(ticket.blocked_by for ticket in snapshot.tickets)


PART_OF_LINK = os.environ.get("FACTORY_GITLAB_PART_OF", "")


@pytest.mark.skipif(not PART_OF_LINK, reason="set FACTORY_GITLAB_PART_OF")
def test_spec_published_with_part_of_lines_has_its_tickets_with_their_blockers_and_no_leading_lines() -> None:
    snapshot = GitLabTracker(PART_OF_LINK).read_spec()

    assert snapshot.tickets
    ids = {ticket.id for ticket in snapshot.tickets}
    for ticket in snapshot.tickets:
        assert re.fullmatch(r"\d+", ticket.id)
        assert not ticket.body.startswith(("Part of", "Blocked by:"))
        assert set(ticket.blocked_by) <= ids
    assert any(ticket.blocked_by for ticket in snapshot.tickets)


def test_number_outside_the_child_tasks_stops_the_read() -> None:
    number = int(LINK.rstrip("/").rsplit("/", 1)[1])

    with pytest.raises(TrackerError, match=f"#{number} is not a child Task"):
        GitLabTracker(LINK, [number]).read_spec()


def test_unknown_project_stops_the_read() -> None:
    host = LINK.split("/")[2]

    with pytest.raises(TrackerError, match="no project"):
        GitLabTracker(f"https://{host}/factory-contract/does-not-exist/-/issues/1").read_spec()


def test_merging_the_mr_into_the_default_branch_closes_the_merged_child_tasks(tmp_path: Path) -> None:
    """Lands an MR that closes the first open child Task, merges it into the default branch and expects it closed.

    It changes the project: it needs FACTORY_GITLAB_LAND=1 on top, and the owner should use a scratch Spec.
    """
    if os.environ.get("FACTORY_GITLAB_LAND") != "1":
        pytest.skip("set FACTORY_GITLAB_LAND=1 to let the test push a branch and merge an MR into the project")
    tracker = GitLabTracker(LINK)
    task = tracker.read_spec().tickets[0].id
    default = tracker.default_branch()
    branch = f"factory-contract/{task}"
    clone = tmp_path / "clone"
    git = ["git", "-c", "user.name=Contract", "-c", "user.email=contract@example.com", "-C", str(clone)]
    subprocess.run(["git", "clone", "--quiet", "--branch", default, tracker.code_url(), str(clone)], check=True)
    subprocess.run([*git, "switch", "--quiet", "-c", branch], check=True)
    (clone / "contract.txt").write_text(f"{branch}\n")
    subprocess.run([*git, "add", "contract.txt"], check=True)
    subprocess.run([*git, "commit", "--quiet", "-m", "Contract test"], check=True)
    subprocess.run([*git, "push", "--quiet", "origin", branch], check=True)
    body = tmp_path / "body.md"
    body.write_text(tracker.footer(tracker.read_spec(), _states(tracker, task), {}))

    url = GitLabPullRequests.of_remote(tracker.code_url()).open(branch, default, "Contract test", body)

    iid = url.rstrip("/").rsplit("/", 1)[1]
    tracker.cli.run("mr", "merge", iid, "--repo", f"{tracker.host}/{tracker.project}", "--yes")
    with pytest.raises(TrackerError, match="is closed"):
        GitLabTracker(LINK, [int(task)]).read_spec()


def _states(tracker: GitLabTracker, merged: str) -> dict[str, TicketState]:
    return {t.id: TicketState.MERGED if t.id == merged else TicketState.QUEUED for t in tracker.read_spec().tickets}


TICKET = os.environ.get("FACTORY_GITLAB_TICKET", "")


def glab(host: str, *args: str) -> Any:
    done = subprocess.run(
        ["glab", "api", "--hostname", host, *args], capture_output=True, text=True, check=True, timeout=60
    )
    return json.loads(done.stdout) if done.stdout.strip() else None


@pytest.mark.skipif(not TICKET, reason="set FACTORY_GITLAB_TICKET")
def test_projection_notes_labels_and_unlabels_an_issue_and_leaves_it_open() -> None:
    tracker = GitLabTracker(LINK)
    path = f"projects/{quote(tracker.project, safe='')}/issues/{TICKET}"
    label = "factory:needs-human"
    before = glab(tracker.host, path)
    notes: list[dict[str, Any]] = []
    try:
        tracker.add_label(TICKET, label)
        tracker.comment(TICKET, 1, "Decision judge waits.")
        assert label in glab(tracker.host, path)["labels"]
        notes = glab(tracker.host, f"{path}/notes?sort=desc&per_page=1")
        assert notes[0]["body"] == "Decision judge waits."

        tracker.remove_label(TICKET, label)
        tracker.remove_label(TICKET, label)

        assert label not in glab(tracker.host, path)["labels"]
        assert glab(tracker.host, path)["state"] == before["state"]
    finally:
        tracker.remove_label(TICKET, label)
        if notes and notes[0]["body"] == "Decision judge waits.":
            glab(tracker.host, "-X", "DELETE", f"{path}/notes/{notes[0]['id']}")


def test_preflight_passes_a_signed_in_glab_that_reads_the_project() -> None:
    tracker = GitLabTracker(LINK)

    assert tracker.preflight({"gitlab": tracker.code_url()}, Path(".")) == []


def test_preflight_reports_a_project_glab_cannot_read() -> None:
    host = LINK.split("/")[2]
    tracker = GitLabTracker(f"https://{host}/factory-contract/does-not-exist/-/issues/1")

    [problem] = tracker.preflight({"gitlab": tracker.code_url()}, Path("."))

    assert problem.startswith(f"glab cannot read factory-contract/does-not-exist on {host}")
