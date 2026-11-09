"""A Run merges its own PR in the Run node `merge_pr`, and the Library's Run graphs `auto-merge` and
`approve-then-merge` use it, the last with a Stage that addresses the comments on the PR."""

import contextlib
import json
import sqlite3
from collections.abc import Callable
from typing import Any, Literal

import pytest

from factory.adapters.github import GitHubPullRequests, is_remote_url
from factory.config import load_project
from factory.pipeline.library_lookup import Layer, Library
from factory.ports.kind import Kind
from factory.ports.tracker import ROLES, CiStatus, FailedJob
from fake_kinds import ACCOUNT, CodeHost
from harness import FACTORY_LIBRARY, NO_RUN_STAGES, AgentScript, Harness, put_graph, run_id_of, ticket_text
from test_ci_wait import (  # noqa: F401
    LOG,
    OLDER,
    PASS,
    RED,
    STALE,
    UNIT,
    Answerer,
    Crash,
    ci_run,
    github,
    killed_runs_run_no_handler,
    on_a_host_without_ci,
    pr_url,
)
from test_describer import DESCRIPTION
from test_run_graph import walked
from test_wait_tracker import APPROVED, CHANGES, PEER, review

READY = "ready-for-agent"
ADDRESSED = {"summary": "Rename the greeting", "handoff": "renamed it"}
FIX = AgentScript(files={"renamed.txt": "renamed\n"}, output=ADDRESSED)
CI_FIX = AgentScript(
    files={"fixed.txt": "fixed\n"},
    output={"summary": "Fix the test", "failure": "a red test", "change": "the test", "handoff": "fixed it"},
)


def merging(factory: Harness, **ci: str) -> None:
    """Writes a Run graph that merges its PR once it left `ci`, by the routes `ci` gives on top of green."""
    graph: dict[str, Any] = json.loads(json.dumps(NO_RUN_STAGES))
    graph["nodes"]["ci"]["next"] |= {"green": "land", **ci}
    graph["nodes"]["land"] = {"kind": "merge_pr", "next": {"merged": "ended", "refused": "ended"}}
    put_graph(factory, "merging", graph)


def graph_with(**land: Any) -> dict[str, Any]:
    graph: dict[str, Any] = json.loads(json.dumps(NO_RUN_STAGES))
    graph["nodes"]["ci"]["next"]["green"] = "land"
    graph["nodes"]["land"] = {"kind": "merge_pr", "next": {"merged": "ended", "refused": "ended"}, **land}
    return graph


def merged_of(factory: Harness, host: str = "github") -> Any:
    """Returns how the host merged the Run's PR, or None when it did not."""
    pulls = factory.gh.pulls() if host == "github" else factory.glab.merge_requests()
    return pulls[0].get("merged")


def refused(factory: Harness, run_id: str) -> list[str]:
    return [event["reason"] for event in factory.events(run_id, "PullRequestMergeRefused")]


@pytest.mark.parametrize("host", ["github", "gitlab"])
def test_auto_merge_merges_the_pr_on_green_ci_and_ends_the_run_done(factory: Harness, host: str) -> None:
    spec = ci_run(factory, host)
    factory.executor.scripts["describe"] = DESCRIPTION
    factory.project["run"] = "auto-merge"
    if host == "github":
        factory.gh.checks([PASS])
    else:
        factory.glab.pipelines({"status": "success"})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-2:] == [("ci", "green"), ("land", "merged")]
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"
    assert merged_of(factory, host) == "merge"
    [merge] = factory.events(run_id, "PullRequestMerged")
    assert merge["url"] == pr_url(host)
    assert f"Pull request: {pr_url(host)}, merged" in result.out
    assert f"merged {pr_url(host)}" in " ".join(factory.cli("status", run_id, "--timeline").out.split())


def test_auto_merge_leaves_a_pr_without_ci_open(factory: Harness) -> None:
    spec = ci_run(factory)
    factory.executor.scripts["describe"] = DESCRIPTION
    factory.project["run"] = "auto-merge"
    factory.gh.set(checks={})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert walked(factory, run_id_of(result.out))[-1] == ("ci", "absent")
    assert merged_of(factory) is None


@pytest.mark.parametrize("host", ["github", "gitlab"])
@pytest.mark.parametrize("method", ["squash", "rebase"])
def test_with_method_is_how_the_host_merges(factory: Harness, host: str, method: str) -> None:
    spec = ci_run(factory, host)
    merging(factory)
    factory.project["overrides"] = {"nodes": {"land": {"with": {"method": method}}}}
    if host == "github":
        factory.gh.checks([PASS])
    else:
        factory.glab.pipelines({"status": "success"})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert merged_of(factory, host) == method


@pytest.mark.parametrize("host", ["github", "gitlab"])
def test_a_pr_the_host_refuses_to_merge_routes_refused_and_the_run_says_why(factory: Harness, host: str) -> None:
    spec = ci_run(factory, host)
    merging(factory)
    reason = "Pull request is not mergeable: the base branch policy prohibits the merge"
    if host == "github":
        factory.gh.checks([PASS])
        factory.gh.set(merge_refused=reason)
    else:
        factory.glab.pipelines({"status": "success"})
        factory.glab.set(mr_merge_refused=reason)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-1] == ("land", "refused")
    assert merged_of(factory, host) is None
    assert reason in refused(factory, run_id)[0]
    assert reason in result.out + result.err
    assert "merge refused" in factory.cli("status", run_id, "--timeline").out


def test_a_host_that_takes_the_merge_and_merges_nothing_routes_refused(factory: Harness) -> None:
    spec = ci_run(factory)
    merging(factory)
    factory.gh.checks([PASS])
    factory.gh.set(merge_queued=True)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-1] == ("land", "refused")
    assert "did not merge" in refused(factory, run_id)[0]


def test_a_head_that_moved_past_the_green_ci_is_not_merged(factory: Harness) -> None:
    spec = ci_run(factory)
    graph = graph_with()
    graph["nodes"]["ci"]["next"]["green"] = "approval"
    graph["nodes"]["approval"] = {
        "kind": "wait_tracker",
        "with": {"on": ["approved"], "timeout": "1h"},
        "next": {"approved": "land", "timeout": "ended"},
    }
    put_graph(factory, "merging", graph)
    factory.gh.checks([PASS])

    def on_sleep(seconds: float) -> None:
        run_ids = factory.run_ids()
        if run_ids and factory.events(run_ids[-1], "TrackerWaitStarted"):
            factory.clock.on_sleep = None
            factory.gh.set(head_oid=OLDER)
            review(factory, "github", APPROVED)

    factory.clock.on_sleep = on_sleep

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-1] == ("land", "refused")
    assert "moved" in refused(factory, run_id)[0]
    assert not [call for call in factory.gh.calls("pr") if call[0] == "merge"]


def test_red_ci_the_graph_routes_to_the_node_is_not_merged(factory: Harness) -> None:
    spec = ci_run(factory)
    merging(factory, red="land")
    factory.gh.checks([RED])
    factory.gh.log(91, LOG)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-1] == ("land", "refused")
    assert "CI" in refused(factory, run_id)[0]
    assert merged_of(factory) is None


def test_red_ci_the_owner_accepted_is_not_merged(factory: Harness) -> None:
    spec = ci_run(factory)
    graph: dict[str, Any] = json.loads(json.dumps(NO_RUN_STAGES))
    graph["nodes"]["ci_failed"]["next"]["accept"] = "land"
    graph["nodes"]["land"] = {"kind": "merge_pr", "next": {"merged": "ended", "refused": "ended"}}
    put_graph(factory, "merging", graph)
    factory.gh.checks([RED])
    factory.gh.log(91, LOG)
    answerer = Answerer(factory, "accept")

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert answerer.codes() == [0]
    assert walked(factory, run_id_of(result.out))[-1] == ("land", "refused")
    assert merged_of(factory) is None


def test_a_merge_of_a_pr_kept_as_a_file_cannot_start_and_check_names_the_role_it_needs(factory: Harness) -> None:
    merging(factory, absent="land")
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("Greet")})
    error = "factory: the node land needs the role Merging, which a PR kept as a file does not serve\n"

    result = factory.run(spec)

    assert (result.code, result.err) == (2, error)
    assert factory.cli("check", str(spec), "--project", str(factory.project_file)).err == error


def test_a_commit_whose_ci_never_showed_within_grace_is_not_merged(factory: Harness) -> None:
    spec = ci_run(factory)
    factory.executor.scripts["describe"] = DESCRIPTION
    merging(factory, absent="land")
    factory.gh.set(checks={})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-2:] == [("ci", "absent"), ("land", "refused")]
    assert "`ci: off`" in refused(factory, run_id)[0]
    assert merged_of(factory) is None


def ci_after_the_wait(factory: Harness, state: Literal["passed", "failed", "pending"]) -> None:
    """Lands on GitHub through a code host whose CI shows no check until the Run journaled it absent, and `state`
    after."""

    class LateCi(GitHubPullRequests):
        def ci(self, pr: str, commit: str) -> CiStatus:
            run_ids = factory.run_ids()
            if not run_ids or not factory.events(run_ids[-1], "CiAbsent"):
                return CiStatus("none")
            return CiStatus(state, [FailedJob("unit", UNIT)] if state == "failed" else [])

    factory.registries.pull_requests.register(
        "github", Kind(lambda url, tools: LateCi.of_remote(url), picks=is_remote_url)
    )


@pytest.mark.parametrize(
    ("state", "outcome", "reason"),
    [
        ("passed", "merged", ""),
        ("failed", "refused", "that started after the wait went red: unit"),
        ("pending", "refused", "is still running, after none showed within `ci.grace`"),
    ],
    ids=["passed", "failed", "pending"],
)
def test_a_merge_reads_again_the_ci_that_showed_no_check_within_grace_and_merges_only_once_it_passed(
    factory: Harness, state: Literal["passed", "failed", "pending"], outcome: str, reason: str
) -> None:
    spec = ci_run(factory)
    factory.executor.scripts["describe"] = DESCRIPTION
    merging(factory, absent="land")
    ci_after_the_wait(factory, state)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-2:] == [("ci", "absent"), ("land", outcome)]
    assert [reason in why for why in refused(factory, run_id)] == ([True] if reason else [])
    assert factory.events(run_id, "CiAbsent")[0]["unseen"] is True


def without_ci(factory: Harness) -> None:
    factory.project["ci"] = "off"


@pytest.mark.parametrize("no_ci", [without_ci, on_a_host_without_ci], ids=["ci_off", "host_runs_no_ci"])
def test_a_project_without_ci_or_a_host_that_runs_none_merges_the_commit_it_pushed(
    factory: Harness, no_ci: Callable[[Harness], None]
) -> None:
    spec = ci_run(factory)
    factory.executor.scripts["describe"] = DESCRIPTION
    merging(factory, absent="land")
    factory.gh.set(checks={})
    no_ci(factory)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-2:] == [("ci", "absent"), ("land", "merged")]
    [merged] = factory.events(run_id, "PullRequestMerged")
    assert merged["commit"] == factory.git("rev-parse", f"factory/{run_id}")


def test_check_refuses_a_merge_on_a_code_host_that_merges_no_pr(factory: Harness) -> None:
    factory.registries.pull_requests.register("plain", CodeHost(factory.remote, merges=False).kind())
    factory.project["code_host"] = "plain"
    factory.write_spec("Greeting", "Say hello.", {"t1": ticket_text("Greet")})
    factory.git("remote", "add", "origin", str(factory.remote))
    merging(factory)

    result = factory.check()

    error = f"the node land needs the role Merging, which the code host of {factory.remote} does not serve"
    assert (result.code, result.err) == (2, f"factory: {error}\n")


def test_a_stack_of_prs_routes_refused(factory: Harness) -> None:
    spec = ci_run(factory)
    merging(factory)
    factory.project["landing"] = "stack"
    factory.gh.checks([PASS])

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-1] == ("land", "refused")
    assert "stack" in refused(factory, run_id)[0]
    assert merged_of(factory) is None


def test_a_pr_merged_already_ends_the_node_merged_with_no_second_merge(factory: Harness) -> None:
    spec = ci_run(factory)
    merging(factory)
    factory.gh.checks([PASS])

    def on_sleep(seconds: float) -> None:
        if factory.gh.pulls():
            factory.clock.on_sleep = None
            data = factory.gh.state()
            data["pulls"][0]["merged"] = "by hand"
            factory.gh.write(data)

    factory.gh.checks([{**PASS, "bucket": "pending"}], [PASS])
    factory.clock.on_sleep = on_sleep

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert walked(factory, run_id_of(result.out))[-1] == ("land", "merged")
    assert merged_of(factory) == "by hand"
    assert not [call for call in factory.gh.calls("pr") if call[0] == "merge"]


def test_a_tracker_that_cannot_be_read_holds_the_node_and_spends_nothing(factory: Harness) -> None:
    spec = ci_run(factory)
    merging(factory)
    factory.gh.checks([PASS])
    factory.gh.set(state_fails=2)

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-1] == ("land", "merged")
    assert len(factory.events(run_id, "TrackerActionFailed")) == 2


def shipped_implementer(factory: Harness) -> None:
    """Gives the Role `implementer` the schema it ships with, where the harness's own requires no `summary`."""
    schema = Library([Layer("factory", FACTORY_LIBRARY)]).schema("implementer").document
    (factory.config / "schemas" / "implementer.json").write_text(json.dumps(schema))


def approving(factory: Harness) -> str:
    """Sets up a GitHub PR for `approve-then-merge` on one Ticket that carries its label."""
    shipped_implementer(factory)
    url = github(factory)
    factory.gh.edit(28, labels=[READY])
    factory.gh.edit(30, labels=[READY])
    factory.executor.scripts["describe"] = DESCRIPTION
    factory.executor.scripts["address"] = FIX
    factory.project["run"] = "approve-then-merge"
    factory.gh.checks([PASS])
    return url


def verdicts(factory: Harness, *said: tuple[str, str | None]) -> None:
    """Gives each verdict, with what the owner wrote along with it, on the next wait for the Tracker."""
    left = list(said)

    def on_sleep(seconds: float) -> None:
        if not factory.run_ids() or not left:
            return
        run_id = factory.run_ids()[-1]
        waits = factory.events(run_id, "TrackerWaitStarted")
        ended = factory.events(run_id, "TrackerWaitEnded")
        if len(waits) == len(said) - len(left) + 1 and len(ended) == len(waits) - 1:
            verdict, text = left.pop(0)
            if text:
                factory.gh.write_on_pull(1, ACCOUNT, text)
            factory.gh.review(1, PEER, verdict)

    factory.clock.on_sleep = on_sleep


def test_approve_then_merge_addresses_one_round_of_comments_then_merges_after_approval(factory: Harness) -> None:
    spec = approving(factory)
    verdicts(factory, (CHANGES["github"], "Rename the greeting."), (APPROVED["github"], None))

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [node for node, _ in walked(factory, run_id)] == [
        "select",
        "claim",
        "build",
        "describe",
        "open_pr",
        "ci",
        "approval",
        "address",
        "ci",
        "approval",
        "land",
    ]
    assert walked(factory, run_id)[-1] == ("land", "merged")
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"
    assert merged_of(factory) == "merge"
    [request] = [request for request in factory.executor.requests if request.stage == "address"]
    inputs = json.loads(request.brief.read_text())["inputs"]
    assert {entry["source"] for entry in inputs} >= {"spec", "pr_comments", "diff"}
    assert not [entry for entry in inputs if entry["source"] == "ci_log"]
    [comments] = [entry["comments"] for entry in inputs if entry["source"] == "pr_comments"]
    assert [comment["body"] for comment in comments] == ["Rename the greeting."]
    [merge] = factory.events(run_id, "PullRequestMerged")
    assert merge["commit"] == factory.events(run_id, "CiFinished")[-1]["commit"]
    assert merge["commit"] != factory.events(run_id, "CiFinished")[0]["commit"]
    assert "Rename the greeting" in factory.gh.pulls()[0]["body"]


def test_check_refuses_an_addresser_that_reads_no_comments_on_the_pr(factory: Harness) -> None:
    approving(factory)
    factory.project["overrides"] = {"nodes": {"address": {"inputs": ["spec"]}}}

    result = factory.check()

    assert result.code == 2
    assert "nodes.address.inputs: a Stage of kind address reads one of pr_comments" in result.err


def test_a_later_round_hears_only_the_comments_no_round_addressed(factory: Harness) -> None:
    spec = approving(factory)
    factory.executor.scripts["address"] = [
        FIX,
        AgentScript(files={"more.txt": "more\n"}, output=ADDRESSED),
    ]
    verdicts(
        factory,
        (CHANGES["github"], "Rename the greeting."),
        (CHANGES["github"], "One more thing."),
        (APPROVED["github"], None),
    )

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    briefs = [json.loads(r.brief.read_text()) for r in factory.executor.requests if r.stage == "address"]
    said = [[comment["body"] for entry in brief["inputs"] for comment in entry.get("comments", [])] for brief in briefs]
    assert said == [["Rename the greeting."], ["One more thing."]]


def test_the_addresser_works_in_a_slot_sandbox_and_directory_of_its_own_and_its_events_name_it(
    factory: Harness,
) -> None:
    spec = approving(factory)
    verdicts(factory, (CHANGES["github"], "Rename the greeting."), (APPROVED["github"], None))
    looks: list[tuple[list[tuple[str, str]], list[str]]] = []

    def look(request: Any) -> None:
        if request.stage == "address":
            looks.append((factory.slots(), factory.cli("status", factory.run_ids()[-1]).out.splitlines()))

    factory.executor.on_start = look

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    for event in ("FixStarted", "FixCommitted", "FixGateFinished", "FixFinished"):
        assert [e["agent"] for e in factory.events(run_id, event)] == ["address"], event
    assert factory.events(run_id, "CiFixStarted") == []
    [(slots, status)] = looks
    assert slots == [(run_id, "/address")]
    assert "Run phase: address" in status
    assert any(
        line.startswith("address: the agent works on the comments on the PR, Round 1, Attempt 1") for line in status
    )
    removed = [(e["agent"], e["sandbox"]) for e in factory.events(run_id, "RunAgentSandboxRemoved")]
    assert ("address", f"address-{run_id}") in removed
    assert (factory.run_dir(run_id) / "address" / "round-1" / "attempt-1" / "brief.json").exists()
    assert not (factory.run_dir(run_id) / "ci_fix").exists()
    assert f"factory/address-{run_id}" not in factory.branches()
    timeline = factory.cli("status", run_id, "--timeline").out.splitlines()
    [line] = [row for row in timeline if row.split()[2] == "address"]
    assert line.split()[2:8] == ["address", "pushed", "round", "1", "attempt", "1"]
    assert (
        "\n\nFixes:\n\n- Round 1: The comments on the PR. Fixed by: Rename the greeting"
        in factory.gh.pulls()[0]["body"]
    )


def test_the_rounds_of_address_and_of_the_ci_fixer_are_counted_apart(factory: Harness) -> None:
    spec = approving(factory)
    factory.project["overrides"] = {"nodes": {"address": {"rounds": {"max": 1, "then": "ended"}}}}
    factory.executor.scripts["ci_fix"] = CI_FIX
    factory.gh.checks([RED], [PASS])
    factory.gh.log(91, LOG)
    verdicts(factory, (CHANGES["github"], "Rename the greeting."), (APPROVED["github"], None))

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert ("address", "done") in walked(factory, run_id)
    assert ("ci_fix", "done") in walked(factory, run_id)
    assert merged_of(factory) == "merge"


def test_an_address_stage_out_of_rounds_ends_the_run_with_the_pr_open(factory: Harness) -> None:
    spec = approving(factory)
    factory.executor.scripts["address"] = AgentScript(output=ADDRESSED)
    factory.project["overrides"] = {"nodes": {"address": {"rounds": {"max": 1, "then": "ended"}}}}
    verdicts(factory, (CHANGES["github"], "Rename the greeting."))

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert walked(factory, run_id)[-2:] == [("address", "failed"), ("address", "exhausted")]
    assert merged_of(factory) is None


def killed_in_address(factory: Harness) -> str:
    """Kills the Run while its Stage `address` works, and returns it orphaned."""
    spec = approving(factory)
    verdicts(factory, (CHANGES["github"], "Rename the greeting."), (APPROVED["github"], None))
    crashes = [True]

    def crash_once(request: Any) -> None:
        if request.stage == "address" and crashes:
            crashes.clear()
            raise Crash

    factory.executor.on_start = crash_once
    with pytest.raises(Crash):
        factory.run(spec)
    factory.crashed()
    factory.clock.sleep(STALE.seconds)
    return factory.run_ids()[-1]


def test_a_run_killed_in_address_resumes_with_a_new_round_on_the_same_comments_and_merges(factory: Harness) -> None:
    run_id = killed_in_address(factory)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    briefs = [json.loads(r.brief.read_text()) for r in factory.executor.requests if r.stage == "address"]
    said = [[comment["body"] for entry in brief["inputs"] for comment in entry.get("comments", [])] for brief in briefs]
    assert said == [["Rename the greeting."], ["Rename the greeting."]]
    assert merged_of(factory) == "merge"


def as_an_older_factory_left_it(factory: Harness, run_id: str) -> None:
    """Leaves the Journal, the Artifacts and the git refs of a Run killed in a Round of the Addresser as a factory left
    them that journaled the Round under the CI fixer's events, slot, Sandbox and directory."""
    with contextlib.closing(sqlite3.connect(factory.home / "factory.db")) as db, db:
        for new, old in (
            ("FixStarted", "CiFixStarted"),
            ("FixCommitted", "CiFixCommitted"),
            ("FixGateFinished", "CiFixGateFinished"),
            ("FixFinished", "CiFixFinished"),
        ):
            db.execute(
                "UPDATE events SET type = ?, payload = json_remove(payload, '$.agent') WHERE run_id = ? AND type = ?",
                (old, run_id, new),
            )
        for agent, prefix in (("describer", "Describer"), ("address", "CiFix")):
            for new, old in (("SandboxRemoved", "SandboxRemoved"), ("SandboxLeftBehind", "SandboxLeftBehind")):
                db.execute(
                    "UPDATE events SET type = ?, payload = json_remove(payload, '$.agent')"
                    " WHERE run_id = ? AND type = ? AND json_extract(payload, '$.agent') = ?",
                    (prefix + old, run_id, "RunAgent" + new, agent),
                )
        db.execute(
            "UPDATE events SET payload = json_set(payload, '$.ticket', '/ci_fix')"
            " WHERE run_id = ? AND json_extract(payload, '$.ticket') = '/address'",
            (run_id,),
        )
        db.execute("UPDATE slots SET ticket_id = '/ci_fix' WHERE run_id = ? AND ticket_id = '/address'", (run_id,))
    (factory.run_dir(run_id) / "address").rename(factory.run_dir(run_id) / "ci_fix")
    workspaces = factory.home / "workspaces"
    (workspaces / f"cifix-{run_id}").mkdir()
    factory.git(
        "worktree",
        "move",
        str(workspaces / f"address-{run_id}" / "workspace"),
        str(workspaces / f"cifix-{run_id}" / "workspace"),
    )
    factory.git("branch", "-m", f"factory/address-{run_id}", f"factory/cifix-{run_id}")


def test_a_run_killed_in_a_round_an_older_factory_journaled_resumes_and_tells_that_round_from_the_next(
    factory: Harness,
) -> None:
    run_id = killed_in_address(factory)
    as_an_older_factory_left_it(factory, run_id)

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    lost, pushed = factory.events(run_id, "FixFinished")
    assert (lost["agent"], lost["round"], lost["cause"]) == ("ci_fix", 1, "lost")
    assert (pushed["agent"], pushed["round"], pushed["outcome"]) == ("address", 2, "pushed")
    [started] = factory.events(run_id, "FixStarted")
    assert (started["agent"], started["round"]) == ("address", 2)
    briefs = [e["brief"] for e in factory.events(run_id, "AttemptStarted") if e["ticket"] in ("/ci_fix", "/address")]
    said = [[comment["body"] for entry in brief["inputs"] for comment in entry.get("comments", [])] for brief in briefs]
    assert said == [["Rename the greeting."], ["Rename the greeting."]]
    assert ("ci_fix", f"cifix-{run_id}") in [
        (e["agent"], e["sandbox"]) for e in factory.events(run_id, "RunAgentSandboxRemoved")
    ]
    assert not [branch for branch in factory.branches() if branch.startswith(("factory/cifix-", "factory/address-"))]
    assert not (factory.home / "workspaces" / f"cifix-{run_id}" / "workspace").exists()
    assert merged_of(factory) == "merge"
    timeline = factory.cli("status", run_id, "--timeline")
    assert timeline.code == 0, timeline.out + timeline.err
    rows = [row.split()[2:6] for row in timeline.out.splitlines() if row.split()[2] in ("ci_fix", "address")]
    assert rows == [["ci_fix", "failed", "round", "1"], ["address", "pushed", "round", "2"]]


def test_finish_of_a_run_killed_in_address_ends_it_done_with_its_pr_open(factory: Harness) -> None:
    run_id = killed_in_address(factory)

    result = factory.cli("finish", run_id)

    assert result.code == 0, result.out + result.err
    assert factory.events(run_id, "RunFinished")[0]["state"] == "done"
    assert merged_of(factory) is None


def test_comments_that_cannot_be_read_hold_the_stage_and_spend_no_round(factory: Harness) -> None:
    spec = approving(factory)
    verdicts(factory, (CHANGES["github"], "Rename the greeting."), (APPROVED["github"], None))
    review_on_github = factory.gh.review

    def review_then_fail(pull: int, author: str, state: str) -> None:
        review_on_github(pull, author, state)
        if state == CHANGES["github"]:
            # The poll that hears the review reads the review comments and the reviews, and the Stage reads next.
            factory.gh.set(comments_pass=2, comments_fail=2)

    factory.gh.review = review_then_fail  # type: ignore[method-assign,assignment]

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    failed = [e for e in factory.events(run_id, "TrackerActionFailed") if e["action"] == "pr_comments"]
    assert failed and all(e["node"] == "address" for e in failed)
    assert [fix["round"] for fix in factory.events(run_id, "FixStarted")] == [1]
    assert merged_of(factory) == "merge"


@pytest.mark.parametrize("name", ["auto-merge", "approve-then-merge"])
def test_the_shipped_run_graphs_pass_check(factory: Harness, name: str) -> None:
    shipped_implementer(factory)
    factory.project["run"] = name

    result = factory.check()

    assert result.code == 0, result.out + result.err


@pytest.mark.parametrize("name", ["default", "planned", "auto-merge", "approve-then-merge"])
def test_every_node_of_a_shipped_run_graph_names_only_roles_the_check_knows(factory: Harness, name: str) -> None:
    shipped_implementer(factory)
    factory.project["run"] = name
    assert factory.check().code == 0
    machine = factory.machine()
    graph = load_project(factory.project_file, machine.library, machine.kinds).run_graph
    assert graph is not None

    roles = {role for node in graph.nodes.values() for role in graph.kind_of(node).roles(node)}

    assert roles <= set(ROLES)


def before_the_ci() -> dict[str, Any]:
    graph = graph_with()
    graph["nodes"]["open_pr"]["next"]["opened"] = "land"
    return graph


def before_the_pr() -> dict[str, Any]:
    graph = graph_with()
    graph["nodes"]["build"]["next"]["all_merged"] = "land"
    return graph


def after_a_failed_pr() -> dict[str, Any]:
    graph = graph_with()
    graph["nodes"]["open_pr"]["next"]["failed"] = "ci"
    return graph


def two_addressers() -> dict[str, Any]:
    graph = graph_with()
    stage = {
        "kind": "address",
        "role": "implementer",
        "inputs": [{"tracker": "pr_comments"}],
        "rounds": {"max": 1, "then": "ended"},
        "next": {"done": "ci", "failed": "ended"},
    }
    graph["nodes"]["ci"]["next"] |= {"red": "one", "timeout": "two"}
    graph["nodes"] |= {"one": stage, "two": stage}
    return graph


@pytest.mark.parametrize(
    ("graph", "error"),
    [
        (before_the_ci, "a path reaches this merge_pr node before the wait_ci node"),
        (before_the_pr, "a path reaches this merge_pr node before the open_pr node"),
        (after_a_failed_pr, "a path reaches this merge_pr node before the open_pr node"),
        (lambda: graph_with(**{"with": {"method": "fast-forward"}}), "must be one of merge, squash, rebase"),
        (lambda: graph_with(**{"with": {"into": "main"}}), "nodes.land.with.into: unknown field"),
        (two_addressers, "a Run graph has at most one node of kind address, and it has one, two"),
    ],
    ids=["before_the_ci", "before_the_pr", "after_a_failed_pr", "unknown_method", "unknown_field", "two_addressers"],
)
def test_check_rejects_a_merge_pr_node_out_of_order_or_with_a_wrong_field(
    factory: Harness, graph: Any, error: str
) -> None:
    shipped_implementer(factory)
    put_graph(factory, "merging", graph())

    result = factory.check()

    assert result.code == 2
    assert error in result.out + result.err
