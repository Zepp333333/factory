"""Seam 3 contract tests check that a real GitLab answers as GitLabTracker and the fake `glab` assume.

They run only with FACTORY_CONTRACT=1 and FACTORY_GITLAB_SPEC=<link>, the issue of a Spec the owner published
on a self-managed project the owner's `glab` can read: open child Tasks, one of which blocks another
by `blocked_by` in the front matter of its description.
Reading changes nothing, so the module creates and closes no issue.
The projection test also needs FACTORY_GITLAB_TICKET=<iid>, a Task of that project it may label and note,
and it deletes the note and the label it adds.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from factory.gitlab import GitLabTracker
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


def test_number_outside_the_child_tasks_stops_the_read() -> None:
    number = int(LINK.rstrip("/").rsplit("/", 1)[1])

    with pytest.raises(TrackerError, match=f"#{number} is not a child Task"):
        GitLabTracker(LINK, [number]).read_spec()


def test_unknown_project_stops_the_read() -> None:
    host = LINK.split("/")[2]

    with pytest.raises(TrackerError, match="no project"):
        GitLabTracker(f"https://{host}/factory-contract/does-not-exist/-/issues/1").read_spec()


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

    assert tracker.preflight({"gitlab": tracker.url()}, Path(".")) == []


def test_preflight_reports_a_project_glab_cannot_read() -> None:
    host = LINK.split("/")[2]
    tracker = GitLabTracker(f"https://{host}/factory-contract/does-not-exist/-/issues/1")

    [problem] = tracker.preflight({"gitlab": tracker.url()}, Path("."))

    assert problem.startswith(f"glab cannot read factory-contract/does-not-exist on {host}")
