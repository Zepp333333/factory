"""Seam 3 contract tests check that a real GitLab answers as GitLabTracker and the fake `glab` assume.

They run only with FACTORY_CONTRACT=1 and FACTORY_GITLAB_SPEC=<link>, the issue of a Spec the owner published
on a self-managed project the owner's `glab` can read: open child Tasks, one of which blocks another
by `blocked_by` in the front matter of its description.
Reading changes nothing, so the module creates and closes no issue.
"""

import os
import re

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
