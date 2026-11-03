import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from factory.adapters.local_tracker import LocalTracker
from factory.core.git import HostGit
from harness import FACTORY_LIBRARY, AgentScript, Harness, git, run_id_of, ticket_text
from test_recovery import STALE, Crash, killed_runs_run_no_handler  # noqa: F401

FINDER_SCHEMA = {
    "x-extends": "issues",
    "type": "object",
    "required": ["summary", "handoff"],
    "properties": {"summary": {"type": "string"}, "handoff": {"type": "string"}},
    "additionalProperties": False,
}

LABELS = ["characterization"]
MILESTONE = "C++ engine characterization coverage"


def issue(key: str, title: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"key": key, "title": title or f"Issue {key}", "body": f"About {key}.", "labels": [], **extra}


def finds(*files: tuple[str, str], issues: list[dict[str, Any]]) -> AgentScript:
    """Returns an agent that writes the files and reports the issues."""
    output = {"summary": "Pin behavior", "handoff": "pinned", "issues": issues}
    return AgentScript(files=dict(files), output=output)


def files_issues(factory: Harness, failed: str = "abandoned", refused: str | None = None) -> None:
    """Routes `implement`, a Stage of Role `finder`, through `file_issues` to `sync` and `merge`."""
    (factory.factory_library / "schemas").mkdir(parents=True, exist_ok=True)
    shutil.copy(FACTORY_LIBRARY / "schemas" / "issues.json", factory.factory_library / "schemas" / "issues.json")
    (factory.config / "schemas" / "finder.json").write_text(json.dumps(FINDER_SCHEMA))
    factory.pipeline["roles"]["finder"] = {"prompt": "prompts/implementer.md", "output": "schemas/finder.json"}
    factory.pipeline["stages"]["implement"].update(role="finder", next={"done": "file_issues"})
    factory.pipeline["operations"]["file_issues"] = {
        "kind": "file_issues",
        "with": {"from": ["implement"], "labels": LABELS, "milestone": MILESTONE},
        "next": {"filed": "sync", "failed": failed},
    }
    if refused is not None:
        factory.pipeline["operations"]["file_issues"]["next"]["refused"] = refused


def merged_file(factory: Harness, run_id: str, name: str) -> str:
    return git(factory.repo, "show", f"factory/{run_id}:{name}")


def local_issues(spec: Path) -> dict[int, str]:
    return {int(path.stem): path.read_text() for path in (spec / "issues").glob("*.md")}


def test_placeholders_become_the_numbers_of_the_issues_filed_in_the_local_tracker(factory: Harness) -> None:
    files_issues(factory)
    spec = factory.write_spec("Pin", "Pin it.", {"a": ticket_text("a")})
    factory.executor.scripts["implement"] = finds(
        (
            "calc.txt",
            "// FINDING #new:ewma-empty: empty input\n// SEAM #new:seam-one: x\n// FINDING #new:ewma-empty: again\n",
        ),
        issues=[issue("ewma-empty", "EWMA of empty input"), issue("seam-one", labels=["seam"])],
    )

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert merged_file(factory, run_id, "calc.txt") == "// FINDING #1: empty input\n// SEAM #2: x\n// FINDING #1: again"
    filed = local_issues(spec)
    assert "title: EWMA of empty input" in filed[1]
    assert "- characterization" in filed[1] and f"milestone: {MILESTONE}" in filed[1]
    assert "- seam" in filed[2]
    assert "factory:run=" in filed[1]


def test_two_tickets_that_report_the_same_finding_share_one_issue_and_the_final_pr_lists_it(factory: Harness) -> None:
    files_issues(factory)
    factory.project["max_parallel"] = 2
    spec = factory.write_spec("Pin", "Pin it.", {name: ticket_text(name) for name in "ab"})
    for name in "ab":
        factory.executor.ticket_scripts[(name, "implement")] = finds(
            (f"{name}.txt", f"// FINDING #new:same: {name}\n"), issues=[issue("same", "The same finding")]
        )

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert sorted(local_issues(spec)) == [1]
    assert [merged_file(factory, run_id, f"{name}.txt") for name in "ab"] == [f"// FINDING #1: {name}" for name in "ab"]
    assert sorted(event["reused"] for event in factory.events(run_id, "IssueFiled")) == [False, True]
    [pull_request] = (spec / "pull_requests").glob("*.md")
    listed = [line for line in pull_request.read_text().splitlines() if line.startswith("- Ticket")]
    assert sorted(line.split(": ", 1)[1] for line in listed) == [
        "#1 The same finding (filed)",
        "#1 The same finding (reused)",
    ]
    assert sorted(line.split(":")[0] for line in listed) == ["- Ticket a", "- Ticket b"]


def test_an_open_issue_with_the_same_title_and_labels_is_reused_and_a_closed_one_is_not(factory: Harness) -> None:
    files_issues(factory)
    spec = factory.write_spec("Pin", "Pin it.", {"a": ticket_text("a")})
    (spec / "issues").mkdir()
    for number, state, labels in [(1, "closed", LABELS), (2, "open", ["other"]), (3, "open", LABELS)]:
        meta = f"title: Known\nstate: {state}\nlabels: {labels}\n"
        (spec / "issues" / f"{number}.md").write_text(f"---\n{meta}---\nOlder.\n")
    factory.executor.scripts["implement"] = finds(("a.txt", "#new:known\n"), issues=[issue("known", "Known")])

    result = factory.run(spec)

    assert result.code == 0, result.out
    assert merged_file(factory, run_id_of(result.out), "a.txt") == "#3"
    assert sorted(local_issues(spec)) == [1, 2, 3]


def test_an_entry_without_a_placeholder_is_filed_all_the_same(factory: Harness) -> None:
    files_issues(factory)
    spec = factory.write_spec("Pin", "Pin it.", {"a": ticket_text("a")})
    factory.executor.scripts["implement"] = finds(("a.txt", "plain\n"), issues=[issue("seam", "A seam")])

    result = factory.run(spec)

    assert result.code == 0, result.out
    assert merged_file(factory, run_id_of(result.out), "a.txt") == "plain"
    assert "title: A seam" in local_issues(spec)[1]


def ended_cause(factory: Harness, spec: Path | str, outcome: str = "refused") -> tuple[str, dict[str, Any]]:
    """Runs the Spec, whose Ticket must end `file_issues` in `outcome`, and returns the Run's id and the cause."""
    result = factory.run(spec)
    run_id = run_id_of(result.out)
    [finished] = [e for e in factory.events(run_id, "OperationFinished") if e["node"] == "file_issues"]
    assert finished["outcome"] == outcome
    assert Path(finished["artifact"]["path"]).name == f"{outcome}-1.json"
    return run_id, json.loads(Path(finished["artifact"]["path"]).read_text())


class Refusal(NamedTuple):
    """A Ticket's agent that leaves one broken placeholder, and the fields and words its refusal carries."""

    script: AgentScript
    fields: dict[str, str]
    reason: str
    files_nothing: bool


# A placeholder without an entry and a key used twice refuse before any issue is filed.
REFUSALS = {
    "missing": Refusal(
        finds(("src/a.txt", "#new:listed #new:missing\n"), issues=[issue("listed", "Listed")]),
        {"key": "missing", "file": "src/a.txt"},
        "#new:missing",
        files_nothing=True,
    ),
    "twice": Refusal(
        finds(("a.txt", "#new:twice\n"), issues=[issue("twice", "One"), issue("twice", "Two")]),
        {"key": "twice"},
        "used twice",
        files_nothing=True,
    ),
    "named": Refusal(
        finds(("#new:named.txt", "text\n"), issues=[issue("named")]),
        {"file": "#new:named.txt"},
        "still has the placeholder #new:named",
        files_nothing=False,
    ),
    "malformed": Refusal(
        finds(("a.txt", "#new:ewma_empty\n"), issues=[issue("ewma")]),
        {"key": "ewma_empty", "file": "a.txt"},
        "ewma_empty",
        files_nothing=False,
    ),
}


def test_a_broken_placeholder_refuses_the_operation_with_its_key_and_file_and_abandons_the_ticket(
    factory: Harness,
) -> None:
    files_issues(factory)
    spec = factory.write_spec("Pin", "Pin it.", {name: ticket_text(name) for name in REFUSALS})
    for name, refusal in REFUSALS.items():
        factory.executor.ticket_scripts[(name, "implement")] = refusal.script

    run_id = run_id_of(factory.run(spec).out)

    titles = {line for text in local_issues(spec).values() for line in text.splitlines() if line.startswith("title:")}
    for name, refusal in REFUSALS.items():
        [finished] = [
            e for e in factory.events(run_id, "OperationFinished") if (e["ticket"], e["node"]) == (name, "file_issues")
        ]
        assert finished["outcome"] == "refused", name
        cause = json.loads(Path(finished["artifact"]["path"]).read_text())
        assert {field: cause[field] for field in refusal.fields} == refusal.fields, name
        assert refusal.reason in cause["reason"], name
        entered = [e["node"] for e in factory.events(run_id, "NodeEntered") if e["ticket"] == name]
        assert entered[-1] == "abandoned", name
        if refusal.files_nothing:
            listed = {f"title: {entry['title']}" for entry in refusal.script.output["issues"]}
            assert not listed & titles, name


def test_a_placeholder_that_cannot_be_replaced_fails_the_operation(factory: Harness) -> None:
    files_issues(factory)
    spec = factory.write_spec("Pin", "Pin it.", {"a": ticket_text("a")})
    factory.executor.scripts["implement"] = AgentScript(
        files={"a.txt": "#new:k\n"},
        run=["chmod 444 a.txt"],
        output={"summary": "Pin", "handoff": "pinned", "issues": [issue("k")]},
    )

    _, cause = ended_cause(factory, spec, "failed")

    assert "a.txt" in cause["reason"]


def test_a_refused_operation_leaves_the_workspace_without_the_numbers_it_put_in(factory: Harness) -> None:
    files_issues(factory, refused="inspect")
    factory.pipeline["stages"]["inspect"] = {"role": "implementer", "next": {"done": "sync"}}
    spec = factory.write_spec("Pin", "Pin it.", {"a": ticket_text("a")})
    factory.executor.scripts["implement"] = finds(
        ("a.txt", "#new:named\n"), ("#new:named.txt", "text\n"), issues=[issue("named")]
    )
    factory.executor.scripts["inspect"] = AgentScript(
        run=["git status --porcelain -- . ':!status.txt' > status.txt"], output={"summary": "Look", "handoff": "looked"}
    )

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert merged_file(factory, run_id, "status.txt") == ""
    assert merged_file(factory, run_id, "a.txt") == "#new:named"


def crash_once(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str, when: Any) -> None:
    """Makes the first call of `owner.name` that `when` accepts raise Crash after it did its work."""
    original = getattr(owner, name)
    crashed: list[bool] = []

    def crashing(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if not crashed and when(*args, **kwargs):
            crashed.append(True)
            raise Crash
        return result

    monkeypatch.setattr(owner, name, crashing)


def resumed(factory: Harness, spec: Path) -> str:
    """Runs the Spec to its Crash, then resumes the Run, which must finish, and returns the Run's id."""
    with pytest.raises(Crash):
        factory.run(spec)
    factory.crashed()
    run_id = factory.run_ids()[-1]
    factory.clock.sleep(STALE.seconds)
    result = factory.cli("resume", run_id)
    assert result.code == 0, result.out + result.err
    return run_id


def test_a_crash_after_filing_and_before_the_journal_does_not_file_the_issue_twice_on_resume(
    factory: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    files_issues(factory)
    spec = factory.write_spec("Pin", "Pin it.", {"a": ticket_text("a")})
    factory.executor.scripts["implement"] = finds(("a.txt", "#new:k\n"), issues=[issue("k", "Found once")])
    crash_once(monkeypatch, LocalTracker, "create_issue", lambda *args, **kwargs: True)

    run_id = resumed(factory, spec)

    assert sorted(local_issues(spec)) == [1]
    assert merged_file(factory, run_id, "a.txt") == "#1"
    [filed] = factory.events(run_id, "IssueFiled")
    assert filed["reused"] is True


def test_a_crash_after_the_journal_and_before_the_commit_does_not_file_the_issue_twice_on_resume(
    factory: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    files_issues(factory)
    spec = factory.write_spec("Pin", "Pin it.", {"a": ticket_text("a")})
    factory.executor.scripts["implement"] = finds(("a.txt", "#new:k\n"), issues=[issue("k", "Found once")])
    crash_once(monkeypatch, HostGit, "files", lambda *args, **kwargs: len(local_issues(spec)) == 1)

    run_id = resumed(factory, spec)

    assert sorted(local_issues(spec)) == [1]
    assert merged_file(factory, run_id, "a.txt") == "#1"
    assert [e["reused"] for e in factory.events(run_id, "IssueFiled")] == [False]


def test_a_journal_that_wrote_the_issue_number_as_an_integer_still_replays_on_resume(
    factory: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    files_issues(factory)
    spec = factory.write_spec("Pin", "Pin it.", {"a": ticket_text("a")})
    factory.executor.scripts["implement"] = finds(("a.txt", "#new:k\n"), issues=[issue("k", "Found once")])
    crash_once(monkeypatch, HostGit, "files", lambda *args, **kwargs: len(local_issues(spec)) == 1)
    with pytest.raises(Crash):
        factory.run(spec)
    factory.crashed()
    run_id = factory.run_ids()[-1]
    with sqlite3.connect(factory.home / "factory.db") as db:
        [(seq, payload)] = db.execute("SELECT seq, payload FROM events WHERE type = 'IssueFiled'").fetchall()
        old = {**{k: v for k, v in json.loads(payload).items() if k != "id"}, "number": 1}
        db.execute("UPDATE events SET payload = ? WHERE seq = ?", (json.dumps(old), seq))
    factory.clock.sleep(STALE.seconds)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert sorted(local_issues(spec)) == [1]
    assert merged_file(factory, run_id, "a.txt") == "#1"
    [finished] = [e for e in factory.events(run_id, "OperationFinished") if e["outcome"] == "filed"]
    assert finished["issues"] == [{"key": "k", "id": "1", "reused": False}]


def github_spec(factory: Harness) -> str:
    """Publishes Spec #21 with the sub-issue #28 and the open milestone, and returns the Spec's link."""
    factory.gh.milestone(3, MILESTONE)
    factory.gh.issue(28, "Pin", "Write a.txt.")
    return factory.gh.issue(21, "Pinning", "Pin it.", sub_issues=[28])


def pinning(factory: Harness) -> None:
    files_issues(factory)
    factory.executor.scripts["implement"] = finds(
        ("a.txt", "// FINDING #new:k\n"), issues=[issue("k", "Found once", labels=["seam"])]
    )


def test_github_files_the_issue_with_the_declared_labels_and_milestone_and_reuses_an_open_one(
    factory: Harness,
) -> None:
    pinning(factory)
    factory.gh.issue(40, "Known elsewhere")
    factory.gh.edit(40, labels=LABELS)
    url = github_spec(factory)

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert merged_file(factory, run_id, "a.txt") == "// FINDING #41"
    filed = factory.gh.issue_state(41)
    assert (filed["title"], filed["labels"], filed["milestone"]) == ("Found once", ["characterization", "seam"], 3)
    assert "<!-- factory:run=" in filed["body"]

    factory.gh.edit(40, title="Found once")
    again = factory.run(url)

    assert again.code == 0, again.out + again.err
    assert [e["reused"] for e in factory.events(run_id_of(again.out), "IssueFiled")] == [True]
    assert factory.gh.issue_state(40)["title"] == "Found once"
    assert merged_file(factory, run_id_of(again.out), "a.txt") == "// FINDING #40"


def test_github_tries_a_failed_call_again(factory: Harness) -> None:
    pinning(factory)
    url = github_spec(factory)
    factory.gh.set(create_fails=2)

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    assert merged_file(factory, run_id_of(result.out), "a.txt") == "// FINDING #29"
    assert len([call for call in factory.gh.calls("api") if "-f" in call and "title=Found once" in call]) == 3


def test_github_fails_the_operation_when_the_error_stays(factory: Harness) -> None:
    pinning(factory)
    url = github_spec(factory)
    factory.executor.scripts["implement"] = finds(("a.txt", "// FINDING #new:k\n"), issues=[issue("k", "Found later")])
    factory.gh.set(create_fails=3)

    _, cause = ended_cause(factory, url, "failed")

    assert "failed 3 times" in cause["reason"]
    assert "Server Error" in cause["reason"]


def test_gitlab_files_the_issue_with_the_declared_labels_and_milestone_and_reuses_an_open_one(
    factory: Harness,
) -> None:
    pinning(factory)
    factory.glab.milestone(5, MILESTONE)
    factory.glab.issue(28, "Pin", "Write a.txt.")
    factory.glab.issue(40, "Found once", kind="Issue")
    factory.glab.edit(40, labels=["characterization"])
    url = factory.glab.issue(21, "Pinning", "Pin it.", kind="Issue", children=[28])

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    assert merged_file(factory, run_id_of(result.out), "a.txt") == "// FINDING #40"

    factory.glab.edit(40, state="closed")
    again = factory.run(url)

    assert again.code == 0, again.out + again.err
    assert merged_file(factory, run_id_of(again.out), "a.txt") == "// FINDING #41"
    filed = factory.glab.issue_state(41)
    assert (filed["labels"], filed["milestone_id"]) == (["characterization", "seam"], 5)
    assert "<!-- factory:run=" in filed["description"]


def test_check_accepts_the_operation(factory: Harness) -> None:
    files_issues(factory)

    result = factory.check()

    assert result.code == 0, result.err


def checked_with(factory: Harness, with_: dict[str, Any]) -> str:
    files_issues(factory)
    factory.pipeline["operations"]["file_issues"]["with"] = with_
    result = factory.check()
    assert result.code == 2
    return result.err


def test_check_refuses_a_from_stage_that_the_pipeline_does_not_declare(factory: Harness) -> None:
    err = checked_with(factory, {"from": ["implement", "nowhere"]})

    assert "operations.file_issues.with.from: names nowhere, which is no Stage of the Pipeline" in err


def test_check_refuses_a_from_that_names_a_gate(factory: Harness) -> None:
    factory.pipeline["gates"] = {
        "build": {"kind": "command", "with": {"run": "true"}, "next": {"pass": "sync", "fail": "abandoned"}}
    }

    err = checked_with(factory, {"from": ["build"]})

    assert "operations.file_issues.with.from: names build, which is no Stage of the Pipeline" in err


def test_check_validates_the_with_of_the_operation(factory: Harness) -> None:
    err = checked_with(factory, {"from": "implement", "labels": [1], "milestone": "", "extra": True})

    assert "operations.file_issues.with.from: must be a list of names" in err
    assert "operations.file_issues.with.labels: must be a list of names" in err
    assert "operations.file_issues.with.milestone: must be the title of a milestone" in err
    assert "extra" in err


def test_check_wants_a_from_in_the_with(factory: Harness) -> None:
    err = checked_with(factory, {})

    assert "operations.file_issues.with.from: must name the Stages whose issues it files" in err


def test_check_refuses_a_with_on_an_operation_that_takes_none(factory: Harness) -> None:
    files_issues(factory)
    factory.pipeline["operations"]["sync"]["with"] = {"from": ["implement"]}

    result = factory.check()

    assert result.code == 2
    assert "operations.sync.with: an Operation of kind sync has no with" in result.err


def test_check_asks_for_a_route_of_each_outcome(factory: Harness) -> None:
    files_issues(factory)
    del factory.pipeline["operations"]["file_issues"]["next"]["filed"]

    result = factory.check()

    assert result.code == 2
    assert "file_issues" in result.err and "filed" in result.err


def test_status_timeline_shows_the_operation_with_the_issues_it_filed(factory: Harness) -> None:
    files_issues(factory)
    spec = factory.write_spec("Pin", "Pin it.", {"a": ticket_text("a")})
    factory.executor.scripts["implement"] = finds(("a.txt", "#new:one #new:two\n"), issues=[issue("one"), issue("two")])
    run_id = run_id_of(factory.run(spec).out)

    result = factory.cli("status", run_id, "--timeline")

    [line] = [line for line in result.out.splitlines() if "file_issues" in line]
    assert line.split()[-3:] == ["filed", "#1,", "#2"]


def reworking(factory: Harness) -> None:
    """Adds the loop `file_issues -> acceptance -> fix -> file_issues`, with `fix` a `finder` too."""
    files_issues(factory)
    (factory.config / "prompts" / "ruler.md").write_text("Rule on the change.\n")
    (factory.config / "schemas" / "ruler.json").write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["outcome"],
                "properties": {"outcome": {"enum": ["accept", "rework"]}, "reasons": {"type": "array"}},
                "additionalProperties": False,
            }
        )
    )
    factory.pipeline["roles"]["ruler"] = {"prompt": "prompts/ruler.md", "output": "schemas/ruler.json"}
    factory.pipeline["operations"]["file_issues"]["with"]["from"] = ["implement", "fix"]
    factory.pipeline["operations"]["file_issues"]["next"]["filed"] = "acceptance"
    factory.pipeline["stages"]["fix"] = {"role": "finder", "inputs": ["cause"], "next": {"done": "file_issues"}}
    factory.pipeline["decisions"] = {
        "acceptance": {
            "decider": {"kind": "model", "role": "ruler"},
            "outcomes": ["accept", "rework"],
            "next": {"accept": "sync", "rework": "fix"},
        }
    }
    factory.executor.scripts["acceptance"] = [
        AgentScript(output={"outcome": "rework", "reasons": ["point the gaps to the issue"]}),
        AgentScript(output={"outcome": "accept"}),
    ]


def test_a_fix_round_that_lists_a_key_again_reuses_the_issue_and_numbers_its_placeholders(factory: Harness) -> None:
    reworking(factory)
    spec = factory.write_spec("Pin", "Pin it.", {"a": ticket_text("a")})
    factory.executor.scripts["implement"] = finds(("a.txt", "// FINDING #new:k\n"), issues=[issue("k", "Found once")])
    factory.executor.scripts["fix"] = finds(("b.txt", "// GAP #new:k\n"), issues=[issue("k", "Found once")])

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    assert sorted(local_issues(spec)) == [1]
    assert merged_file(factory, run_id, "a.txt") == "// FINDING #1"
    assert merged_file(factory, run_id, "b.txt") == "// GAP #1"
    assert [e["id"] for e in factory.events(run_id, "IssueFiled")] == ["1"]
    assert [e["node"] for e in factory.events(run_id, "NodeEntered")].count("acceptance") == 2


def test_the_latest_stage_output_wins_the_entry_of_a_key_and_no_issue_is_edited(factory: Harness) -> None:
    reworking(factory)
    spec = factory.write_spec("Pin", "Pin it.", {"a": ticket_text("a")})
    factory.executor.scripts["implement"] = finds(("a.txt", "#new:k\n"), issues=[issue("k", "Found once")])
    factory.executor.scripts["fix"] = finds(("b.txt", "#new:k\n"), issues=[issue("k", "Found again")])

    result = factory.run(spec)

    assert result.code == 0, result.out
    assert sorted(local_issues(spec)) == [1]
    assert "title: Found once" in local_issues(spec)[1]


def test_a_key_that_file_issues_synced_repeats_after_file_issues_is_one_issue(factory: Harness) -> None:
    reworking(factory)
    factory.pipeline["operations"]["file_issues"]["with"]["from"] = ["implement"]
    factory.pipeline["operations"]["file_issues_synced"] = {
        "kind": "file_issues",
        "with": {"from": ["implement", "fix"], "labels": LABELS, "milestone": MILESTONE},
        "next": {"filed": "merge", "failed": "abandoned"},
    }
    factory.pipeline["decisions"]["acceptance"]["next"]["accept"] = "merge"
    factory.pipeline["stages"]["fix"]["next"] = {"done": "file_issues_synced"}
    factory.executor.scripts["acceptance"] = [AgentScript(output={"outcome": "rework"})]
    spec = factory.write_spec("Pin", "Pin it.", {"a": ticket_text("a")})
    factory.executor.scripts["implement"] = finds(("a.txt", "#new:k\n"), issues=[issue("k", "Found once")])
    factory.executor.scripts["fix"] = finds(("b.txt", "#new:k\n"), issues=[issue("k", "Found again")])

    result = factory.run(spec)

    assert result.code == 0, result.out
    assert sorted(local_issues(spec)) == [1]
    assert "title: Found once" in local_issues(spec)[1]
    assert merged_file(factory, run_id_of(result.out), "b.txt") == "#1"


def test_a_refusal_goes_to_the_fix_stage_as_its_cause_and_the_next_filing_files_the_issue(factory: Harness) -> None:
    files_issues(factory, refused="fix")
    factory.pipeline["operations"]["file_issues"]["with"]["from"] = ["implement", "fix"]
    factory.pipeline["stages"]["fix"] = {"role": "finder", "inputs": ["cause"], "next": {"done": "file_issues"}}
    spec = factory.write_spec("Pin", "Pin it.", {"a": ticket_text("a")})
    factory.executor.scripts["implement"] = finds(("a.txt", "// SEAM #new:a\n"), issues=[issue("b", "Other")])
    factory.executor.scripts["fix"] = finds(issues=[issue("a", "Seam")])

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    outcomes = [e["outcome"] for e in factory.events(run_id, "OperationFinished") if e["node"] == "file_issues"]
    assert outcomes == ["refused", "filed"]
    [entered] = [e for e in factory.events(run_id, "NodeEntered") if e["node"] == "fix"]
    assert Path(entered["cause"]["path"]).name == "refused-1.json"
    assert merged_file(factory, run_id, "a.txt") == "// SEAM #2"


def test_check_accepts_refused_to_a_stage_that_reads_the_cause(factory: Harness) -> None:
    files_issues(factory, refused="fix")
    factory.pipeline["stages"]["fix"] = {"role": "finder", "inputs": ["cause"], "next": {"done": "file_issues"}}

    result = factory.check()

    assert result.code == 0, result.err
