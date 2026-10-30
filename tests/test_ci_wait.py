import subprocess
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from factory.config import display_path
from factory.custody import HEARTBEAT_STALE_SECONDS
from factory.orchestrator import Orchestrator
from harness import REPO, Harness, run_id_of
from test_github_run import APPEND, publish
from test_gitlab_land import MR_REPO, landing

TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
STALE = timedelta(seconds=HEARTBEAT_STALE_SECONDS + 1)
UNIT = "https://github.com/Owner/target/actions/runs/5/job/91"
PASS = {"name": "unit", "bucket": "pass", "link": UNIT}
RUNNING = {"name": "unit", "bucket": "pending", "link": UNIT}
RED = {"name": "unit", "bucket": "fail", "link": UNIT}
LOG = "FAILED tests/test_clock.py::test_run_lasts_0s - assert 1 == 0"


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
    factory.executor.scripts["implement"] = APPEND
    return publish(factory)


def crash_in_ci(factory: Harness, url: str) -> str:
    """Crashes the Run once it waits for CI, and returns it orphaned."""

    def on_sleep(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "CiStarted"):
            factory.clock.on_sleep = None
            raise Crash

    factory.clock.on_sleep = on_sleep
    with pytest.raises(Crash):
        factory.run(url)
    factory.clock.sleep(STALE.seconds)
    return factory.run_ids()[-1]


def test_run_ends_done_once_the_checks_of_the_pushed_commit_pass(factory: Harness) -> None:
    url = github(factory)
    factory.gh.checks([RUNNING], [RUNNING], [PASS])
    held: list[list[tuple[str, str]]] = []

    def look(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "CiStarted"):
            held.append(factory.slots())

    factory.clock.on_sleep = look

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    pushed = factory.git("rev-parse", f"factory/{run_id}")
    [started] = factory.events(run_id, "CiStarted")
    assert (started["commit"], started["url"]) == (pushed, f"https://github.com/{REPO}/pull/1")
    [finished] = factory.events(run_id, "CiFinished")
    assert finished == {"outcome": "passed", "commit": pushed, "jobs": []}
    assert factory.events(run_id, "CiAbsent") == []
    assert len(factory.gh.calls("pr")) > 3
    assert result.out.splitlines()[-2] == f"Run {run_id} finished: done in {factory.run_took(run_id)}, $0.00, CI passed"
    assert held and all(slots == [] for slots in held)


def test_pr_without_checks_within_the_grace_period_ends_the_run_with_ci_absent(factory: Harness) -> None:
    url = github(factory)
    factory.project["ci"] = {"grace": "90s"}

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [absent] = factory.events(run_id, "CiAbsent")
    assert absent["grace"] == 90
    assert factory.events(run_id, "CiFinished") == []
    assert factory.times(run_id, "CiAbsent")[0] - factory.times(run_id, "CiStarted")[0] >= timedelta(seconds=90)
    assert "CI checks" in result.out.splitlines()[-2]


def after_grace(factory: Harness, then: Callable[[], None]) -> None:
    """Calls `then` once the Run has waited for CI past the default grace period."""

    def on_sleep(seconds: float) -> None:
        started = factory.run_ids() and factory.times(factory.run_ids()[-1], "CiStarted")
        if started and factory.clock.now() - started[0] > timedelta(minutes=4):
            factory.clock.on_sleep = None
            then()

    factory.clock.on_sleep = on_sleep


def test_checks_of_an_older_commit_do_not_end_the_wait(factory: Harness) -> None:
    url = github(factory)
    factory.gh.set(head_oid="0" * 40)
    factory.gh.checks([PASS])
    after_grace(factory, lambda: factory.gh.set(head_oid=""))

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert factory.events(run_id, "CiAbsent") == []
    [finished] = factory.events(run_id, "CiFinished")
    assert finished["outcome"] == "passed"
    assert factory.times(run_id, "CiFinished")[0] - factory.times(run_id, "CiStarted")[0] > timedelta(minutes=4)


def test_red_ci_opens_ci_failed_with_the_jobs_and_accept_ends_the_run_done_with_the_red_ci_recorded(
    factory: Harness,
) -> None:
    url = github(factory)
    factory.gh.checks([RUNNING], [RED])
    factory.gh.log(91, LOG)
    answerer = Answerer(factory, "accept")

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [finished] = factory.events(run_id, "CiFinished")
    assert (finished["outcome"], finished["jobs"]) == ("failed", [{"name": "unit", "url": UNIT}])
    assert LOG in Path(finished["artifact"]).read_text()
    [waiting] = factory.events(run_id, "DecisionWaiting")
    assert (waiting["ticket"], waiting["node"], waiting["outcomes"]) == ("@run", "ci_failed", ["recheck", "accept"])
    card = Path(waiting["card"]).read_text()
    assert f"unit: {UNIT}" in card and f"factory decide {run_id} @run <outcome>" in card
    assert answerer.codes() == [0]
    assert [e["outcome"] for e in factory.events(run_id, "DecisionDecided")] == ["accept"]
    assert [e["state"] for e in factory.events(run_id, "RunFinished")] == ["done"]
    assert result.out.splitlines()[-2].endswith(", $0.00, CI failed, accepted by the owner")
    assert factory.waiting() == []


def test_recheck_waits_for_the_ci_of_the_current_head_again(factory: Harness) -> None:
    url = github(factory)
    fixed = "f" * 40
    factory.gh.checks([RED])
    factory.gh.checks([PASS], sha=fixed)
    factory.gh.log(91, LOG)
    answerer = Answerer(factory, "recheck")
    answer = factory.clock.on_sleep
    assert answer is not None

    def push_a_fix(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "DecisionWaiting"):
            factory.gh.set(head_oid=fixed)
        answer(seconds)

    factory.clock.on_sleep = push_a_fix

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    assert answerer.codes() == [0]
    run_id = run_id_of(result.out)
    assert [e["outcome"] for e in factory.events(run_id, "CiFinished")] == ["failed", "passed"]
    pushed = factory.git("rev-parse", f"factory/{run_id}")
    assert [e["commit"] for e in factory.events(run_id, "CiStarted")] == [pushed, fixed]
    assert result.out.splitlines()[-2].endswith(", CI passed")


def test_ci_that_does_not_end_in_time_opens_ci_failed(factory: Harness) -> None:
    url = github(factory)
    factory.project["ci"] = {"timeout": "10m"}
    factory.gh.checks([RUNNING])
    Answerer(factory, "accept")

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [finished] = factory.events(run_id, "CiFinished")
    assert finished["outcome"] == "timeout"
    assert factory.times(run_id, "CiFinished")[0] - factory.times(run_id, "CiStarted")[0] >= timedelta(minutes=10)
    assert result.out.splitlines()[-2].endswith(", CI timed out, accepted by the owner")


def test_ci_off_ends_the_run_without_asking_the_tracker_about_checks(factory: Harness) -> None:
    url = github(factory)
    factory.project["ci"] = "off"

    result = factory.run(url)

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


def test_status_shows_phase_ci_and_the_commit_the_run_waits_for(factory: Harness) -> None:
    url = github(factory)
    factory.gh.checks([RUNNING], [PASS])
    seen: list[str] = []

    def look(seconds: float) -> None:
        if factory.run_ids() and factory.events(factory.run_ids()[-1], "CiStarted") and not seen:
            seen.append(factory.cli("status", factory.run_ids()[-1]).out)

    factory.clock.on_sleep = look

    result = factory.run(url)

    run_id = run_id_of(result.out)
    short = factory.git("rev-parse", f"factory/{run_id}")[:7]
    assert "Run phase: ci" in seen[0].splitlines()
    assert f"ci: waits for CI on {short}, PR #1, 0s" in seen[0].splitlines()


def test_resume_of_a_run_orphaned_in_phase_ci_goes_on_waiting_for_the_same_commit(factory: Harness) -> None:
    url = github(factory)
    factory.gh.checks([RUNNING], [PASS])
    run_id = crash_in_ci(factory, url)
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
    url = github(factory)
    factory.gh.checks([RUNNING])
    run_id = crash_in_ci(factory, url)

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


def snapshot(*jobs: dict[str, Any], status: str) -> dict[str, Any]:
    return {"status": status, "jobs": list(jobs)}


JOB = {"id": 11, "name": "unit", "web_url": f"https://{MR_REPO}/-/jobs/11", "trace": LOG}


def test_gitlab_run_ends_done_once_the_head_pipeline_of_the_mr_passes(factory: Harness) -> None:
    url = landing(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.glab.pipelines(snapshot(status="running"), snapshot(status="success"))

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [e["outcome"] for e in factory.events(run_id, "CiFinished")] == ["passed"]
    assert result.out.splitlines()[-2].endswith(", CI passed")


def test_gitlab_pipeline_that_stops_at_a_manual_job_passes(factory: Harness) -> None:
    url = landing(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.glab.pipelines(snapshot(status="running"), snapshot(status="manual"))

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    assert [e["outcome"] for e in factory.events(run_id, "CiFinished")] == ["passed"]


def test_gitlab_failed_pipeline_opens_ci_failed_with_the_trace_of_its_failed_jobs(factory: Harness) -> None:
    url = landing(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.glab.pipelines(snapshot(JOB, status="failed"))
    Answerer(factory, "accept")

    result = factory.run(url)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [finished] = factory.events(run_id, "CiFinished")
    assert finished["jobs"] == [{"name": "unit", "url": JOB["web_url"]}]
    assert LOG in Path(finished["artifact"]).read_text()


def test_gitlab_pipeline_of_an_older_commit_does_not_count(factory: Harness) -> None:
    url = landing(factory)
    factory.executor.scripts["implement"] = APPEND
    factory.glab.set(mr_head="0" * 40)
    factory.glab.pipelines(snapshot(status="success"))
    after_grace(factory, lambda: factory.glab.set(mr_head=""))

    result = factory.run(url)

    run_id = run_id_of(result.out)
    assert result.code == 0, result.out + result.err
    assert factory.events(run_id, "CiAbsent") == []
    assert [e["outcome"] for e in factory.events(run_id, "CiFinished")] == ["passed"]
    assert factory.times(run_id, "CiFinished")[0] - factory.times(run_id, "CiStarted")[0] > timedelta(minutes=4)


def test_spec_folder_on_a_github_remote_opens_its_pr_on_github_and_waits_for_its_ci(factory: Harness) -> None:
    factory.detach_remotes = False
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = APPEND
    factory.gh.checks([RUNNING], [PASS])

    result = factory.run(spec)

    assert result.code == 0, result.out + result.err
    run_id = run_id_of(result.out)
    [opened] = factory.events(run_id, "PullRequestOpened")
    assert opened["url"] == f"https://github.com/{REPO}/pull/1"
    assert f"factory/{run_id}" in factory.remote_branches()
    assert not (spec / "pull_requests").exists()
    [finished] = factory.events(run_id, "CiFinished")
    assert finished["outcome"] == "passed"
