# A Tracker kind serves narrow roles, and each consumer takes only its role

The Tracker port was one protocol of eleven members that served four consumers with four jobs.
`factory run` reads the Spec and asks where it lives, the landing writes the PR text with the footer that closes the merged Tickets, the Projection comments and labels the Tickets, and `file_issues` finds and files issues.
A new Tracker had to implement all of them, even one that cannot label or file an issue, and each consumer could call every member.
#230 left this split open, because it was a decision of its own.

So `ports/tracker.py` declares five roles:

- `SpecSource`: `read_spec`, `code_url` and `pull_request_folder`, for `factory run` and `Machine.pull_requests_of`.
- `Closing`: `reference`, `describe`, `footer` and `defuse`, for the landing, the CI fixer and the text of `file_issues`, and `reference` for the Run tree and `factory status`.
- `TicketBoard`: `comment`, `add_label` and `remove_label`, for the Projection, and with `assign` and `spec_id` for the Tracker Operations.
- `IssueDesk`: `find_issues` and `create_issue`, for `file_issues`.
- `Discussions`: `owner`, `ticket_comments` and `pr_comments`, for a Stage with a Tracker input.

A Tracker kind's `make` returns a `Tracker`, the value that holds the roles the kind serves.
Every kind serves a `SpecSource` and a `Closing`, because a Run cannot start without its Spec or land without its PR text, and a kind leaves `board` or `desk` None when it cannot serve them.
The `RunContext` holds the `Closing`, the `RunTools` hold the `TicketBoard` and the `IssueDesk`, and `Operations` hands the `IssueDesk` to `IssueFiling`, so no consumer takes the whole Tracker.
A Run on a Tracker without a `TicketBoard` shows its Decisions only in the terminal and projects nothing.
`LocalTracker` serves a `TicketBoard` for its Projection and its `assign` does nothing, since a folder has no accounts.
A Pipeline with an Operation `file_issues` fails `factory check` and the start of `factory run` on a Tracker without an `IssueDesk`, and the error names the Operation and the role.
A Pipeline or Run graph with an Operation `assign`, `label` or `comment` does so on a Tracker without a `TicketBoard`, and one with a Tracker input on a Tracker without `Discussions`.
GitHub and GitLab serve `Discussions`, and `LocalTracker` does not, since a folder holds nothing an owner writes between two Attempts.
The factory works with the owner's access, so an author cannot tell its comments from the owner's: each `TicketBoard.comment` of GitHub and GitLab ends in a signature that a rendered comment hides, and `Discussions` reads it as `by_factory`.

The reasons hold without any agent harness:

- GitLab serves every role, but its labels and its closing keywords differ from GitHub's, and a role is the unit a second code host reimplements and tests.
- The Spec folder serves every role through files, but its `Closing` closes nothing, and the roles show which of its files stand in for what a host does.
- A read-only Tracker, such as a Spec in a wiki or a document store, or a Tracker whose token may not write, can read the Spec and write the PR text but cannot label or file an issue.
  It serves `SpecSource` and `Closing` and runs any Pipeline without `file_issues`.

## Considered Options

- Keep one protocol, and let a Tracker that cannot label raise or do nothing.
  Rejected because a Pipeline with `file_issues` would then fail in the middle of a Run, after its Attempts were paid for, instead of at `factory check`.
- Declare the roles a kind serves as a field of its `Kind`, and cast the adapter to each role.
  Rejected because every registry shares one `Kind` contract, and a cast tells mypy nothing about what the adapter implements.
- Check the roles with `isinstance` on runtime-checkable protocols.
  Rejected because such a check reads only the names of the members, and no module outside `adapters/` checks the type of an adapter.
- Split `PullRequests` and CI status out as well.
  Out of scope: `PullRequests` is a port of its own already, which lands the PR and reads its CI.

## Consequences

- An adapter of a new Tracker implements only the roles it can serve, and the contract of the Tracker port skips the tests of a role the kind does not serve.
- A Pipeline that needs a role names it through its Operations, so a new Operation that needs a role adds its check next to the one of `file_issues` in `Machine.problems`.
- A Run that resumes takes the Tracker its kind makes then, and `factory run` checked its roles at the start.
