"""The contract of the Tracker port, which every Tracker kind of `wiring` and a fake kind of `tests/` pass.

The harness registers no Tracker in the place of a real kind: `GitHubTracker` and `GitLabTracker` talk to the fake `gh`
and `glab`, and the `Notebook` of `fake_kinds` is a kind only the tests know.
Each kind has a Bench in `FAKED`, which publishes the same Spec on the kind's fake and reads back what it holds.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from factory.core.states import TicketState
from factory.ports.kind import Kind
from factory.ports.tracker import Issue, IssueDesk, SpecLink, TicketBoard, TicketSnapshot, Tracker, TrackerError
from factory.wiring import REGISTRIES
from fake_kinds import ACCOUNT, Notebook
from harness import Harness, ticket_text
from hosts import Host, Picked, fake_of, host, kinds  # noqa: F401

CLOSES = re.compile(r"^Closes #(\S+)$", re.MULTILINE)
LABEL = "factory:needs-human"
CARRIED = ["needs-triage", "bug"]
OTHER = "mona"


@dataclass
class Bench:
    """A Bench publishes Spec `Greeting`, whose Ticket `Greet again` waits on `Greet`, on the fake of one kind."""

    factory: Harness
    has_accounts = True

    def publish(self) -> str:
        """Publishes the Spec, with `Greet` carrying no label and `Greet again`, which it blocks, carrying `CARRIED`."""
        raise NotImplementedError

    def comments(self, ticket: str) -> list[str]:
        raise NotImplementedError

    def labels(self, ticket: str) -> list[str]:
        raise NotImplementedError

    def assignees(self, ticket: str) -> list[str]:
        raise NotImplementedError

    def hand_to(self, ticket: str, account: str) -> None:
        """Puts `account` among the assignees of the Ticket."""
        raise NotImplementedError

    def fail_assignment(self, how: str) -> None:
        """Makes the next assignment fail: the Tracker `ignored` it, or says whom it works as with `no_account` or
        `no_object`."""
        raise NotImplementedError

    def milestone(self, title: str) -> None:
        """Opens the milestone `title`, on a Tracker that keeps milestones."""

    def closed(self, text: str) -> set[str]:
        """Returns the Tickets that a PR with `text` closes on its merge."""
        return set(CLOSES.findall(text))

    def kind(self, name: str) -> Kind[Tracker]:
        """Returns the kind the Bench publishes on, which is the one `wiring` registers under `name`."""
        return REGISTRIES.trackers[name]


class GitHub(Bench):
    def publish(self) -> str:
        self.factory.gh.issue(28, "Greet", "Write hello.txt.")
        self.factory.gh.issue(30, "Greet again", "Write bye.txt.", blocked_by=[28], labels=CARRIED)
        return self.factory.gh.issue(21, "Greeting", "Say hello.", sub_issues=[28, 30])

    def comments(self, ticket: str) -> list[str]:
        return list(self.issue(ticket).get("comments", []))

    def labels(self, ticket: str) -> list[str]:
        return list(self.issue(ticket).get("labels", []))

    def milestone(self, title: str) -> None:
        self.factory.gh.milestone(4, title)

    def assignees(self, ticket: str) -> list[str]:
        return list(self.issue(ticket).get("assignees", []))

    def hand_to(self, ticket: str, account: str) -> None:
        self.factory.gh.edit(int(ticket), assignees=[*self.assignees(ticket), account])

    def fail_assignment(self, how: str) -> None:
        if how == "ignored":
            self.factory.gh.set(assign_ignored=True)
        else:
            self.factory.gh.set(user={} if how == "no_account" else [])

    def issue(self, ticket: str) -> dict[str, Any]:
        return self.factory.gh.issue_state(int(ticket))


class GitLab(Bench):
    def publish(self) -> str:
        self.factory.glab.issue(28, "Greet", "Write hello.txt.")
        self.factory.glab.issue(30, "Greet again", "---\nblocked_by: [28]\n---\nWrite bye.txt.", labels=CARRIED)
        return self.factory.glab.issue(21, "Greeting", "Say hello.", kind="Issue", children=[28, 30])

    def comments(self, ticket: str) -> list[str]:
        return list(self.factory.glab.issue_state(int(ticket)).get("notes", []))

    def labels(self, ticket: str) -> list[str]:
        return list(self.factory.glab.issue_state(int(ticket)).get("labels", []))

    def assignees(self, ticket: str) -> list[str]:
        return list(self.factory.glab.issue_state(int(ticket)).get("assignees", []))

    def hand_to(self, ticket: str, account: str) -> None:
        self.factory.glab.edit(int(ticket), assignees=[*self.assignees(ticket), account])

    def fail_assignment(self, how: str) -> None:
        if how == "ignored":
            self.factory.glab.set(assign_ignored=True)
        else:
            self.factory.glab.set(user={} if how == "no_account" else [])

    def milestone(self, title: str) -> None:
        self.factory.glab.milestone(4, title)


class Local(Bench):
    has_accounts = False

    def publish(self) -> str:
        tickets = {"t1": ticket_text("Greet"), "t2": ticket_text("Greet again", ["t1"])}
        return str(self.factory.write_spec("Greeting", "Say hello.", tickets, {"t2": CARRIED}))

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


class InNotebook(Bench):
    LINK = "notebook:greeting"

    notebook: Notebook

    def publish(self) -> str:
        tickets = [
            TicketSnapshot("1", "Greet", "Write hello.txt."),
            TicketSnapshot("2", "Greet again", "Write bye.txt.", ["1"]),
        ]
        self.notebook = Notebook(self.LINK, "Greeting", "Say hello.", tickets, labels={"2": list(CARRIED)})
        return self.LINK

    def comments(self, ticket: str) -> list[str]:
        return self.notebook.comments.get(ticket, [])

    def labels(self, ticket: str) -> list[str]:
        return self.notebook.labels.get(ticket, [])

    def assignees(self, ticket: str) -> list[str]:
        return self.notebook.assignees.get(ticket, [])

    def hand_to(self, ticket: str, account: str) -> None:
        self.notebook.assignees.setdefault(ticket, []).append(account)

    def fail_assignment(self, how: str) -> None:
        self.notebook.assign_error = how

    def kind(self, name: str) -> Kind[Tracker]:
        return self.notebook.kind()


FAKED: dict[str, type[Bench]] = {"github": GitHub, "gitlab": GitLab, "local": Local}

# The kinds only the tests know, which `wiring` does not register.
TEST_KINDS: dict[str, type[Bench]] = {"notebook": InNotebook}


@dataclass
class Published:
    """The Spec on the fake of a kind, read by the kind's Tracker of its `link`, which `select` remakes with ids."""

    bench: Bench
    tracker: Tracker
    link: str
    select: Callable[[list[str]], Tracker]


@pytest.fixture(params=[*kinds(REGISTRIES.trackers), *(Picked("tests", name) for name in TEST_KINDS)], ids=str)
def published(request: pytest.FixtureRequest, host: Host) -> Published:  # noqa: F811
    """Publishes the Spec on the kind's fake and makes the kind's Tracker of its link."""
    name = request.param.name
    bench = fake_of(request.param, "trackers", {**FAKED, **TEST_KINDS})(host.factory)
    link = bench.publish()
    kind = bench.kind(name)
    return Published(
        bench, kind.make(SpecLink(link), host.tools), link, lambda ids: kind.make(SpecLink(link, ids), host.tools)
    )


def board(published: Published) -> TicketBoard:
    """Returns the TicketBoard of the kind, and skips a kind that does not serve one."""
    if published.tracker.board is None:
        pytest.skip("the kind serves no TicketBoard")
    return published.tracker.board


def assigning_board(published: Published) -> TicketBoard:
    """Returns the TicketBoard of a kind with accounts, and skips a kind without them, which assigns nothing."""
    if not published.bench.has_accounts:
        pytest.skip("the kind has no accounts")
    return board(published)


def desk(published: Published) -> IssueDesk:
    """Returns the IssueDesk of the kind, and skips a kind that does not serve one."""
    if published.tracker.desk is None:
        pytest.skip("the kind serves no IssueDesk")
    return published.tracker.desk


def test_read_spec_snapshots_the_spec_and_its_tickets_with_their_blockers(published: Published) -> None:
    snapshot = published.tracker.spec.read_spec()

    assert (snapshot.title, snapshot.body) == ("Greeting", "Say hello.")
    first, second = snapshot.tickets
    assert [(t.title, t.blocked_by) for t in snapshot.tickets] == [("Greet", []), ("Greet again", [first.id])]
    assert "Write" in first.body


def test_read_spec_reports_the_labels_of_each_ticket(published: Published) -> None:
    first, second = published.tracker.spec.read_spec().tickets

    assert (first.labels, second.labels) == ([], CARRIED)


def test_a_tracker_made_with_ticket_ids_reads_only_those_and_keeps_a_blocker_it_did_not_read(
    published: Published,
) -> None:
    first, second = published.tracker.spec.read_spec().tickets

    assert [t.id for t in published.select([first.id]).spec.read_spec().tickets] == [first.id]
    [read] = published.select([second.id]).spec.read_spec().tickets
    assert read.id == second.id
    assert read.blocked_by == [first.id]


def test_a_comment_lands_on_its_ticket_with_its_text(published: Published) -> None:
    ticket = published.tracker.spec.read_spec().tickets[0].id

    board(published).comment(ticket, 7, "Decision review waits.")

    assert published.bench.comments(ticket) == ["Decision review waits."]


def test_a_label_is_added_once_and_removed_and_removing_it_again_is_no_error(published: Published) -> None:
    ticket = published.tracker.spec.read_spec().tickets[0].id

    board(published).add_label(ticket, LABEL)
    board(published).add_label(ticket, LABEL)
    assert published.bench.labels(ticket) == [LABEL]

    board(published).remove_label(ticket, LABEL)
    board(published).remove_label(ticket, LABEL)
    assert published.bench.labels(ticket) == []


def test_the_spec_takes_a_comment_and_a_label_by_its_id_as_a_ticket_does(published: Published) -> None:
    tickets = board(published)
    spec = tickets.spec_id()

    tickets.comment(spec, 8, "Run 1 takes this Spec.")
    tickets.add_label(spec, LABEL)

    assert published.bench.comments(spec) == ["Run 1 takes this Spec."]
    assert published.bench.labels(spec) == [LABEL]
    assert spec not in {ticket.id for ticket in published.tracker.spec.read_spec().tickets}


def test_assign_adds_the_account_the_tracker_works_as_once_and_keeps_the_other_assignees(published: Published) -> None:
    tickets = assigning_board(published)
    ticket = published.tracker.spec.read_spec().tickets[0].id
    published.bench.hand_to(ticket, OTHER)

    assert tickets.assign(ticket) == ACCOUNT
    assert tickets.assign(ticket) is None
    assert published.bench.assignees(ticket) == [OTHER, ACCOUNT]


@pytest.mark.parametrize("how", ["ignored", "no_account", "no_object"])
def test_assign_raises_when_the_account_does_not_land_on_the_ticket(published: Published, how: str) -> None:
    tickets = assigning_board(published)
    ticket = published.tracker.spec.read_spec().tickets[0].id
    published.bench.fail_assignment(how)

    with pytest.raises(TrackerError):
        tickets.assign(ticket)
    assert published.bench.assignees(ticket) == []


def test_a_created_issue_is_found_by_its_labels_and_only_by_them(published: Published) -> None:
    published.bench.milestone("Hardening")

    filed = desk(published).create_issue("Flaky clock", "The clock test is flaky.", ["finding", "ci"], "Hardening")

    found = desk(published).find_issues(["finding", "ci"])
    assert Issue(filed, "Flaky clock", "The clock test is flaky.") in found
    assert filed not in [issue.id for issue in desk(published).find_issues(["finding", "other"])]


def test_the_footer_closes_exactly_the_merged_tickets_and_defuse_closes_none(published: Published) -> None:
    snapshot = published.tracker.spec.read_spec()
    first, second = snapshot.tickets
    states = {first.id: TicketState.MERGED, second.id: TicketState.ABANDONED}

    footer = published.tracker.closing.footer(snapshot, states, {})
    described = published.tracker.closing.describe(snapshot, "r1", states, {})

    assert published.bench.closed(footer) == published.bench.closed(described.body) == {first.id}
    assert published.tracker.closing.reference(second.id) in footer
    assert published.bench.closed(published.tracker.closing.defuse(f"Closes #{second.id}\n")) == set()
