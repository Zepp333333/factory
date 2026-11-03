"""The contract of the Tracker port, which every Tracker kind of `wiring` passes.

The harness registers no Tracker of its own: `GitHubTracker` and `GitLabTracker` talk to the fake `gh` and `glab`.
Each kind has a Bench in `FAKED`, which publishes the same Spec on the kind's fake and reads back what it holds.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from factory.core.states import TicketState
from factory.ports.tracker import Issue, SpecLink, Tracker, TrackerError
from factory.wiring import REGISTRIES
from harness import Harness, ticket_text
from hosts import Host, fake_of, host, kinds  # noqa: F401

CLOSES = re.compile(r"^Closes #(\S+)$", re.MULTILINE)
LABEL = "factory:needs-human"


@dataclass
class Bench:
    """A Bench publishes Spec `Greeting`, whose Ticket `Greet again` waits on `Greet`, on the fake of one kind."""

    factory: Harness

    def publish(self) -> str:
        raise NotImplementedError

    def comments(self, ticket: str) -> list[str]:
        raise NotImplementedError

    def labels(self, ticket: str) -> list[str]:
        raise NotImplementedError

    def milestone(self, title: str) -> None:
        """Opens the milestone `title`, on a Tracker that keeps milestones."""

    def closed(self, text: str) -> set[str]:
        """Returns the Tickets that a PR with `text` closes on its merge."""
        return set(CLOSES.findall(text))


class GitHub(Bench):
    def publish(self) -> str:
        self.factory.gh.issue(28, "Greet", "Write hello.txt.")
        self.factory.gh.issue(30, "Greet again", "Write bye.txt.", blocked_by=[28])
        return self.factory.gh.issue(21, "Greeting", "Say hello.", sub_issues=[28, 30])

    def comments(self, ticket: str) -> list[str]:
        return list(self.issue(ticket).get("comments", []))

    def labels(self, ticket: str) -> list[str]:
        return list(self.issue(ticket).get("labels", []))

    def milestone(self, title: str) -> None:
        self.factory.gh.milestone(4, title)

    def issue(self, ticket: str) -> dict[str, Any]:
        return self.factory.gh.issue_state(int(ticket))


class GitLab(Bench):
    def publish(self) -> str:
        self.factory.glab.issue(28, "Greet", "Write hello.txt.")
        self.factory.glab.issue(30, "Greet again", "---\nblocked_by: [28]\n---\nWrite bye.txt.")
        return self.factory.glab.issue(21, "Greeting", "Say hello.", kind="Issue", children=[28, 30])

    def comments(self, ticket: str) -> list[str]:
        return list(self.factory.glab.issue_state(int(ticket)).get("notes", []))

    def labels(self, ticket: str) -> list[str]:
        return list(self.factory.glab.issue_state(int(ticket)).get("labels", []))

    def milestone(self, title: str) -> None:
        self.factory.glab.milestone(4, title)


class Local(Bench):
    def publish(self) -> str:
        tickets = {"t1": ticket_text("Greet"), "t2": ticket_text("Greet again", ["t1"])}
        return str(self.factory.write_spec("Greeting", "Say hello.", tickets))

    def comments(self, ticket: str) -> list[str]:
        return [path.read_text() for path in sorted(self.projection(ticket).glob("comments/*.md"))]

    def labels(self, ticket: str) -> list[str]:
        path = self.projection(ticket) / "labels"
        return path.read_text().splitlines() if path.is_file() else []

    def closed(self, text: str) -> set[str]:
        """Returns the Tickets the PR lists as merged, since a Spec folder has no issue to close."""
        merged = text.partition("Merged:")[2].partition("Not landed:")[0]
        return set(re.findall(r"^- (\S+) ", merged, re.MULTILINE))

    def projection(self, ticket: str) -> Path:
        return self.factory.tracker / "spec" / "projection" / ticket


FAKED: dict[str, type[Bench]] = {"github": GitHub, "gitlab": GitLab, "local": Local}


@dataclass
class Published:
    """The Spec on the fake of a kind, read by the kind's Tracker of its `link`, which `select` remakes with ids."""

    bench: Bench
    tracker: Tracker
    link: str
    select: Callable[[list[str]], Tracker]


@pytest.fixture(params=kinds(REGISTRIES.trackers), ids=str)
def published(request: pytest.FixtureRequest, host: Host) -> Published:  # noqa: F811
    """Publishes the Spec on the kind's fake and makes the kind's Tracker of its link."""
    name = request.param.name
    bench = fake_of(request.param, "trackers", FAKED)(host.factory)
    link = bench.publish()
    kind = REGISTRIES.trackers[name]
    return Published(
        bench, kind.make(SpecLink(link), host.tools), link, lambda ids: kind.make(SpecLink(link, ids), host.tools)
    )


def test_read_spec_snapshots_the_spec_and_its_tickets_with_their_blockers(published: Published) -> None:
    snapshot = published.tracker.read_spec()

    assert (snapshot.title, snapshot.body) == ("Greeting", "Say hello.")
    first, second = snapshot.tickets
    assert [(t.title, t.blocked_by) for t in snapshot.tickets] == [("Greet", []), ("Greet again", [first.id])]
    assert "Write" in first.body


def test_a_tracker_made_with_ticket_ids_reads_only_those_and_refuses_one_whose_blocker_it_does_not_take(
    published: Published,
) -> None:
    first, second = published.tracker.read_spec().tickets

    assert [t.id for t in published.select([first.id]).read_spec().tickets] == [first.id]
    with pytest.raises(TrackerError, match=f"{second.id} .*blocked by .*{first.id}, which this Run does not take"):
        published.select([second.id]).read_spec()


def test_a_comment_lands_on_its_ticket_with_its_text(published: Published) -> None:
    ticket = published.tracker.read_spec().tickets[0].id

    published.tracker.comment(ticket, 7, "Decision review waits.")

    assert published.bench.comments(ticket) == ["Decision review waits."]


def test_a_label_is_added_once_and_removed_and_removing_it_again_is_no_error(published: Published) -> None:
    ticket = published.tracker.read_spec().tickets[0].id

    published.tracker.add_label(ticket, LABEL)
    published.tracker.add_label(ticket, LABEL)
    assert published.bench.labels(ticket) == [LABEL]

    published.tracker.remove_label(ticket, LABEL)
    published.tracker.remove_label(ticket, LABEL)
    assert published.bench.labels(ticket) == []


def test_a_created_issue_is_found_by_its_labels_and_only_by_them(published: Published) -> None:
    published.bench.milestone("Hardening")

    filed = published.tracker.create_issue("Flaky clock", "The clock test is flaky.", ["finding", "ci"], "Hardening")

    found = published.tracker.find_issues(["finding", "ci"])
    assert Issue(filed, "Flaky clock", "The clock test is flaky.") in found
    assert filed not in [issue.id for issue in published.tracker.find_issues(["finding", "other"])]


def test_the_footer_closes_exactly_the_merged_tickets_and_defuse_closes_none(published: Published) -> None:
    snapshot = published.tracker.read_spec()
    first, second = snapshot.tickets
    states = {first.id: TicketState.MERGED, second.id: TicketState.ABANDONED}

    footer = published.tracker.footer(snapshot, states, {})
    described = published.tracker.describe(snapshot, "r1", states, {})

    assert published.bench.closed(footer) == published.bench.closed(described.body) == {first.id}
    assert published.tracker.reference(second.id) in footer
    assert published.bench.closed(published.tracker.defuse(f"Closes #{second.id}\n")) == set()
