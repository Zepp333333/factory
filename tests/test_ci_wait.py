import json
import re
import subprocess
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from factory.adapters.github import GitHubPullRequests, is_remote_url
from factory.config import display_path
from factory.journal.custody import HEARTBEAT_STALE_SECONDS
from factory.ports.kind import Kind
from factory.ports.tracker import CiStatus
from factory.run.orchestrator import Orchestrator
from harness import NO_RUN_STAGES, REPO, Harness, put_graph, run_id_of
from test_github_run import APPEND, publish

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
STALE = timedelta(seconds=HEARTBEAT_STALE_SECONDS + 1)
UNIT = "https://github.com/Owner/target/actions/runs/5/job/91"
PASS = {"name": "unit", "bucket": "pass", "link": UNIT}
RUNNING = {"name": "unit", "bucket": "pending", "link": UNIT}
RED = {"name": "unit", "bucket": "fail", "link": UNIT}
LOG = "FAILED tests/test_clock.py::test_run_lasts_0s - assert 1 == 0"
MR_REPO = "gitlab.example.com/group/sub/project"
OLDER = "0" * 40


class Crash(BaseException):
    """A Crash stands for `kill -9` of the Run's process, which runs no handler."""


@pytest.fixture(autouse=True)
def killed_runs_run_no_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    abort = Orchestrator.abort

    def abort_unless_crashed(self: Orchestrator, reason: str) -> None:
        if reason != "Crash":
            abort(self, reason)

    monkeypatch.setattr(Orchestrator, "abort", abort_unless_crashed)


class Answerer:
    """An Answerer answers each Decision of the Run, in turn, with `factory decide` from the Run's sleep."""

    def __init__(self, factory: Harness, *outcomes: str) -> None:
        self.factory = factory
        self.outcomes = list(outcomes)
        self.procs: list[subprocess.Popen[str]] = []
        factory.clock.on_sleep = self.on_sleep

    def on_sleep(self, seconds: float) -> None:
        if not self.factory.run_ids():
            return
        run_id = self.factory.run_ids()[-1]
        if len(self.factory.events(run_id, "DecisionWaiting")) > len(self.procs):
            self.procs.append(self.factory.spawn("decide", run_id, "@run", self.outcomes[len(self.procs)]))

    def codes(self) -> list[int | None]:
        for proc in self.procs:
            proc.communicate(timeout=30)
        return [proc.returncode for proc in self.procs]


def github(factory: Harness) -> str:
    """Publishes Spec #21 on GitHub with agents that append a line, so its Run waits for CI on a GitHub PR."""
    factory.executor.scripts["implement"] = APPEND
    return publish(factory)


def ci_run(factory: Harness, host: str = "github") -> Path:
    """Writes a Spec folder of one Ticket whose project lands through `host`, so the Run waits for the CI of its PR.

    The CI phase depends only on the PullRequests, so a Spec folder saves the Tracker's calls.
    """
    factory.detach_remotes = False
    if host.startswith("gitlab"):
        factory.git("remote", "remove", "gitlab")
        factory.git("remote", "set-url", "origin", f"git@{MR_REPO.replace('/', ':', 1)}.git")
        host_name, project = MR_REPO.split("/", 1)
        factory.glab.set(hosts={host_name: {"projects": {project: {"items": {}}}}})
    factory.executor.scripts["implement"] = APPEND
    return factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})


def pr_url(host: str) -> str:
    return f"https://github.com/{REPO}/pull/1" if host == "github" else f"https://{MR_REPO}/-/merge_requests/1"


def crash_in_ci(factory: Harness, url: Path | str) -> str:
    """Crashes the Run once it waits for CI, and returns it orphaned."""

    def on_sleep(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "CiStarted"):
            factory.clock.on_sleep = None
            raise Crash

    factory.clock.on_sleep = on_sleep
    with pytest.raises(Crash):
        factory.run(url)
    factory.crashed()
    factory.clock.sleep(STALE.seconds)
    return factory.run_ids()[-1]


def after_grace(factory: Harness, then: Callable[[], None]) -> None:
    """Calls `then` once the Run has waited for CI past the default grace period."""

    def on_sleep(seconds: float) -> None:
        started = factory.run_ids() and factory.times(factory.run_ids()[-1], "CiStarted")
        if started and factory.clock.now() - started[0] > timedelta(minutes=4):
            factory.clock.on_sleep = None
            then()

    factory.clock.on_sleep = on_sleep


def snapshot(*jobs: dict[str, Any], status: str) -> dict[str, Any]:
    return {"status": status, "jobs": list(jobs)}


JOB = {"id": 11, "name": "unit", "web_url": f"https://{MR_REPO}/-/jobs/11", "trace": LOG}


def head_moves_on_after_grace(factory: Harness, host: str) -> None:
    """Gives the PR an older head commit than the pushed one until the grace period is over, with CI that passes.

    GitHub's checks run and then pass, a GitLab pipeline succeeds, and with `gitlab-manual` it stops at a manual job.
    """
    if host == "github":
        factory.gh.set(head_oid=OLDER)
        factory.gh.checks([RUNNING], [PASS])
        after_grace(factory, lambda: factory.gh.set(head_oid=""))
    else:
        factory.glab.set(mr_head=OLDER)
        factory.glab.pipelines(snapshot(status="manual" if host == "gitlab-manual" else "success"))
        after_grace(factory, lambda: factory.glab.set(mr_head=""))


@pytest.mark.parametrize("host", ["github", "gitlab", "gitlab-manual"])
def test_ci_of_an_older_head_does_not_count_and_the_run_ends_done_once_the_pushed_commit_passes(
    factory: Harness, host: str
) -> None:
    spec = ci_run(factory, host)
    head_moves_on_after_grace(factory, host)
    wait_after_grace = factory.clock.on_sleep
    assert wait_after_grace is not None
    held: list[list[tuple[str, str]]] = []
    seen: list[str] = []

    def look(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "CiStarted"):
            held.append(factory.slots())
            if not seen:
                seen.append(factory.cli("status", factory.run_ids()[-1]).out)
        wait_after_grace(seconds)

    factory.clock.on_sleep = look

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    pushed = factory.git("rev-parse", f"factory/{run_id}")
    [opened] = factory.events(run_id, "PullRequestOpened")
    assert opened["url"] == pr_url(host)
    assert f"factory/{run_id}" in factory.remote_branches()
    assert not (spec / "pull_requests").exists()
    [started] = factory.events(run_id, "CiStarted")
    assert (started["commit"], started["url"]) == (pushed, pr_url(host))
    [finished] = factory.events(run_id, "CiFinished")
    assert finished == {"outcome": "passed", "commit": pushed, "jobs": []}
    assert factory.events(run_id, "CiAbsent") == []
    assert factory.times(run_id, "CiFinished")[0] - factory.times(run_id, "CiStarted")[0] > timedelta(minutes=4)
    assert result.out.splitlines()[-2] == f"Run {run_id} finished: done in {factory.run_took(run_id)}, $0.00, CI passed"
    assert held and all(slots == [] for slots in held)
    assert "Run phase: ci" in seen[0].splitlines()
    assert any(re.fullmatch(rf"ci: waits for CI on {pushed[:7]}, PR #1, \d+s", line) for line in seen[0].splitlines())


def test_pr_without_checks_within_the_grace_period_ends_the_run_with_ci_absent(factory: Harness) -> None:
    spec = ci_run(factory)
    factory.project["ci"] = {"grace": "90s"}

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [absent] = factory.events(run_id, "CiAbsent")
    assert absent["grace"] == 90
    assert factory.events(run_id, "CiFinished") == []
    assert factory.times(run_id, "CiAbsent")[0] - factory.times(run_id, "CiStarted")[0] >= timedelta(seconds=90)
    assert "CI checks" in result.out.splitlines()[-2]


class NoCi(GitHubPullRequests):
    """NoCi lands on GitHub like the kind `github`, on a code host that runs no CI."""

    def ci(self, pr: str, commit: str) -> CiStatus:
        return CiStatus("absent")


def test_a_code_host_whose_ci_is_absent_ends_the_run_without_waiting(factory: Harness) -> None:
    spec = ci_run(factory)
    factory.registries.pull_requests.register(
        "github", Kind(lambda url, tools: NoCi.of_remote(url), picks=is_remote_url)
    )

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [started] = factory.events(run_id, "CiStarted")
    assert factory.events(run_id, "CiAbsent") == [{"commit": started["commit"]}]
    assert factory.events(run_id, "CiFixStarted") == []
    assert "CI checks" in result.out.splitlines()[-2]


def test_failed_pipeline_opens_ci_failed_with_the_trace_of_its_jobs_and_accept_ends_the_run_with_red_ci_recorded(
    factory: Harness,
) -> None:
    spec = ci_run(factory, "gitlab")
    factory.glab.pipelines(snapshot(status="running"), snapshot(JOB, status="failed"))
    job = JOB["web_url"]
    answerer = Answerer(factory, "accept")

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [finished] = factory.events(run_id, "CiFinished")
    assert (finished["outcome"], finished["jobs"]) == ("failed", [{"name": "unit", "url": job}])
    assert LOG in Path(finished["artifact"]).read_text()
    [waiting] = factory.events(run_id, "DecisionWaiting")
    assert (waiting["ticket"], waiting["node"], waiting["outcomes"]) == ("@run", "ci_failed", ["recheck", "accept"])
    card = Path(waiting["card"]).read_text()
    assert f"unit: {job}" in card and f"factory decide {run_id} @run <outcome>" in card
    assert answerer.codes() == [0]
    assert [e["outcome"] for e in factory.events(run_id, "DecisionDecided")] == ["accept"]
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]
    assert result.out.splitlines()[-2].endswith(", $0.00, CI failed, accepted by the owner")
    assert [(e["node"], e["outcome"]) for e in factory.events(run_id, "RunNodeEnded")][-2:] == [
        ("ci", "red"),
        ("ci_failed", "accept"),
    ]
    assert factory.waiting() == []


def test_red_checks_open_ci_failed_with_their_jobs_and_recheck_waits_for_the_current_head_until_it_times_out(
    factory: Harness,
) -> None:
    spec = ci_run(factory)
    factory.project["ci"] = {"timeout": "2m"}
    fixed = "f" * 40
    factory.gh.checks([RUNNING], [RED])
    factory.gh.checks([RUNNING], sha=fixed)
    factory.gh.log(91, LOG)
    answerer = Answerer(factory, "recheck", "accept")
    answer = factory.clock.on_sleep
    assert answer is not None

    def push_a_fix(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "DecisionWaiting"):
            factory.gh.set(head_oid=fixed)
        answer(seconds)

    factory.clock.on_sleep = push_a_fix

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    assert answerer.codes() == [0, 0]
    run_id = run_id_of(result.out)
    assert [e["outcome"] for e in factory.events(run_id, "DecisionDecided")] == ["recheck", "accept"]
    red, timeout = factory.events(run_id, "CiFinished")
    assert (red["outcome"], red["jobs"], timeout["outcome"]) == ("failed", [{"name": "unit", "url": UNIT}], "timeout")
    assert LOG in Path(red["artifact"]).read_text()
    asked, _ = factory.events(run_id, "DecisionWaiting")
    assert (asked["ticket"], asked["node"], asked["outcomes"]) == ("@run", "ci_failed", ["recheck", "accept"])
    card = Path(asked["card"]).read_text()
    assert f"unit: {UNIT}" in card and f"factory decide {run_id} @run <outcome>" in card
    pushed = factory.git("rev-parse", f"factory/{run_id}")
    assert [e["commit"] for e in factory.events(run_id, "CiStarted")] == [pushed, fixed]
    timed_out = factory.times(run_id, "CiFinished")[1] - factory.times(run_id, "CiStarted")[1]
    assert timed_out >= timedelta(minutes=2)
    assert result.out.splitlines()[-2].endswith(", CI timed out, accepted by the owner")


def test_ci_off_ends_the_run_without_asking_the_tracker_about_checks(factory: Harness) -> None:
    spec = ci_run(factory)
    factory.project["ci"] = "off"

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "CiStarted") == []
    assert factory.gh.calls("pr") == [call for call in factory.gh.calls("pr") if call[0] == "create"]
    assert result.out.splitlines()[-2] == f"Run {run_id} finished: done in {factory.run_took(run_id)}, $0.00"


def test_run_from_a_local_tracker_ends_as_before(factory: Harness) -> None:
    factory.executor.scripts["implement"] = APPEND
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "CiStarted") == []
    assert f"finished: done in {factory.run_took(run_id)}, $0.00\n" in result.out


def test_resume_of_a_run_orphaned_in_phase_ci_goes_on_waiting_for_the_same_commit(factory: Harness) -> None:
    spec = ci_run(factory)
    factory.gh.checks([RUNNING], [PASS])
    run_id = crash_in_ci(factory, spec)
    [first] = factory.events(run_id, "CiStarted")

    result = factory.cli("resume", run_id)

    assert result.code == 0, result.out + result.err
    assert [e["commit"] for e in factory.events(run_id, "CiStarted")] == [first["commit"]] * 2
    assert len(factory.gh.pulls()) == 1
    assert len(factory.events(run_id, "PullRequestOpened")) == 1
    assert [e["outcome"] for e in factory.events(run_id, "CiFinished")] == ["passed"]
    assert f"Run {run_id} finished: done in" in result.out and "CI passed" in result.out
    assert f"Pull request: https://github.com/{REPO}/pull/1" in result.out


def test_finish_of_a_run_orphaned_in_phase_ci_ends_it_without_waiting(factory: Harness) -> None:
    spec = ci_run(factory)
    factory.gh.checks([RUNNING])
    run_id = crash_in_ci(factory, spec)

    result = factory.cli("finish", run_id)

    assert result.code == 0, result.out + result.err
    [finished] = factory.events(run_id, "CiFinished")
    assert finished["outcome"] == "unchecked"
    assert len(factory.gh.pulls()) == 1
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]
    assert ", CI not checked" in result.out


def test_check_validates_ci(factory: Harness) -> None:
    factory.project["ci"] = {"timeout": "soon", "grace": 0, "wait": 1}
    factory.machine()

    result = factory.check()

    prefix = f"factory: {display_path(factory.project_file)}: "
    assert sorted(line.removeprefix(prefix) for line in result.err.splitlines()) == [
        "ci.grace: must be a duration such as 90s, 45m or 2h",
        "ci.timeout: must be a duration such as 90s, 45m or 2h",
        "ci.wait: unknown field",
    ]
    factory.project["ci"] = "off"
    factory.machine()
    assert factory.check().code == 0


def test_an_outcome_named_abort_of_a_decision_node_routes_the_walk_and_does_not_abort_the_run(factory: Harness) -> None:
    spec = ci_run(factory)
    graph: dict[str, Any] = json.loads(json.dumps(NO_RUN_STAGES))
    graph["nodes"]["ci_failed"] = {
        "decider": {"kind": "human"},
        "outcomes": {"abort": "Drop this CI and build the Tickets again", "accept": "End the Run as it is"},
        "next": {"abort": "build", "accept": "ended"},
    }
    put_graph(factory, "rebuilding", graph)
    factory.gh.checks([RED], [PASS])
    factory.gh.log(91, LOG)
    answerer = Answerer(factory, "abort")

    result = factory.run(spec)

    assert answerer.codes() == [0]
    run_id = run_id_of(result.out)
    assert [(e["node"], e["outcome"]) for e in factory.events(run_id, "RunNodeEnded")][-4:] == [
        ("ci_failed", "abort"),
        ("build", "all_merged"),
        ("open_pr", "opened"),
        ("ci", "green"),
    ]
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]
    assert result.code == 0, result.out + result.err
