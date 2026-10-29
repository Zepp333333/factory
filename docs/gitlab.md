# Publish a Spec in GitLab and run it

`factory run` reads a Spec from GitLab Issues on a self-managed instance or on gitlab.com through `GitLabTracker`.
It calls the owner's `glab` on the host with the link's host as `--hostname`, and no agent in a Sandbox reaches GitLab.
A waiting human Decision is projected into the Ticket's issue.
A Run that merged a Ticket lands as a merge request (below).

## Set up `glab` once

Install `glab` and sign in to the instance the Spec lives on:

```sh
glab auth login --hostname gitlab.example.com
```

The token needs the `api` scope.

## Publish a Spec

- The Spec is an issue.
- Its Tickets are its child Tasks, from the work item hierarchy that the Free tier has.
  Add a Task under the issue with "Add" - "Existing task" or "New task" in the issue's Child items.
- A Ticket's id is its `iid`, the number after `#` in GitLab.
- An order between Tickets goes in a `blocked_by` list in YAML front matter at the top of the Ticket's description:

  ```markdown
  ---
  blocked_by: [28, 29]
  ---
  Write bye.txt.
  ```

  The Free tier has no blocking links, so this works everywhere.
  Quote a reference written with `#`, as in `["#28"]`, because YAML reads an unquoted `#` as a comment.
- Where the instance has native "is blocked by" links (Premium and up), `GitLabTracker` reads them too and joins them with the front matter.
- The front matter does not reach the Ticket's body in the snapshot.
- A closed blocker counts as done.
  An open blocker that is not a Ticket of the Run stops `factory run` before it starts.

## Run

Pass the link to the Spec's issue, of the shape `https://<host>/<group>/.../<project>/-/issues/<iid>`
or `.../-/work_items/<iid>`, with any host and nested groups:

```sh
factory run https://gitlab.example.com/group/sub/project/-/issues/21
```

`--tickets 28,30` takes only those child Tasks, and a number that is not an open child Task of the Spec stops the Run before it starts.
A malformed front matter, a blocker that names no issue, a GitLab access error or a missing `glab` also stop `factory run`
before the Run and the Integration branch exist.
`resume` and `finish` rebuild `GitLabTracker` from the Spec's link in the Run's journal.

## Land a Run as a merge request

A Run that merged a Ticket pushes its Integration branch to the git remote of the Spec's project,
found among the Target project's remotes over HTTPS or SSH, with nested groups, a custom SSH port and a self-managed host.
Then it opens an MR into `base` through `glab mr create` and prints the MR's link last.
If the push or the MR fails, the Run journals `LandingFailed` and prints the commands that finish the landing by hand.

The MR text closes each merged Ticket with `Closes #<iid>` and lists the others with why they did not land.
The Describer's text goes through GitLab's closing pattern first: `Close`, `Fix`, `Resolve` and `Implement` in all their forms,
before `#N`, `group/project#N` and full `/-/issues/` or `/-/work_items/` links, become `Refs`, so only the footer closes Tickets.

GitLab closes issues only on a merge into the project's default branch.
`factory run` warns before the Run starts when `base` is not that branch, because the merged Tickets will stay open.

## Questions in GitLab

When a Ticket waits for a human Decision, the Run adds the question card as a note on the Ticket's issue and the label `factory:needs-human`.
After the answer it removes the label and adds a note with the outcome.
Removing a label the issue no longer holds is no error, and closing the issue does not affect the Run.
A call that fails is retried through the Journal cursor, and each call waits at most 15 seconds.
The token needs the `api` scope and the right to edit the project's issues.

## Check `glab` before a Run

`factory check <link>` and `factory run <link>` check, before anything starts, that `glab` is installed,
signed in to the link's host and able to read the Spec's project, and that a git remote of the Target project points at that project.
Each problem names its fix, such as `glab auth login --hostname gitlab.example.com` or `git remote add gitlab https://gitlab.example.com/group/project.git`.
`factory check` without a link checks the project file and its Pipeline only.

## Check the contract with GitLab

`tests/contract/test_gitlab.py` runs against a Spec on a real project and changes nothing there, except the projection test, which labels and notes one Task and removes both again.
Publish a Spec with open child Tasks, one of which has a `blocked_by` front matter, and run:

```sh
FACTORY_GITLAB_SPEC=https://gitlab.example.com/group/project/-/issues/21 \
  make test-contract ARGS=tests/contract/test_gitlab.py
```

Add `FACTORY_GITLAB_LAND=1` to also push a branch, open an MR, merge it into the default branch and check
that the Task it closes is closed. Use a scratch Spec for it.

To run the projection test too, add `FACTORY_GITLAB_TICKET=<iid>` with a Task of that project it may label and note.
