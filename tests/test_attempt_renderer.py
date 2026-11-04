import functools
import io
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from factory.ports.executor import EVENTS, attempt_event, event_line, read_event
from factory.render import Follower
from harness import AgentScript, Harness, run_id_of

FIXTURE = Path(__file__).parent / "fixtures" / "streams" / "session.ndjson"
TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def render(events: Path) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "factory.render", str(events)], check=True, capture_output=True, text=True, timeout=30
    ).stdout
    return ANSI.sub("", out).splitlines()


def old_attempt(root: Path) -> Path:
    """Returns the directory of an Attempt from before the Executor wrote its events, with only the fixture stream."""
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURE, root / "stream.ndjson")
    return root


@functools.cache
def rendered_fixture() -> list[str]:
    """Renders the fixture stream once for every test that only reads what the renderer printed."""
    with tempfile.TemporaryDirectory() as root:
        return render(old_attempt(Path(root)) / EVENTS)


def test_renderer_shows_text_tool_calls_short_results_rate_limits_and_the_result() -> None:
    lines = rendered_fixture()

    assert "I will list the files." in lines
    assert '> Bash {"command": "ls -la"}' in lines
    result = next(line for line in lines if line.startswith("  < README.md"))
    assert len(result) < 200 and result.endswith("…")
    assert "  < This command requires approval" in lines
    assert "rate limit allowed_warning resets 1790000000" in lines
    assert "Done." in lines
    assert "result success turns 3 4200 ms" in lines
    assert "All done." in lines


def test_renderer_skips_empty_thinking_blocks_and_shows_the_others() -> None:
    lines = rendered_fixture()

    assert [line for line in lines if line.startswith("thinking")] == ["thinking: Weighing the options"]


def test_renderer_shows_what_the_executor_could_not_read_and_skips_what_shows_nothing(tmp_path: Path) -> None:
    events = tmp_path / EVENTS
    events.write_bytes(
        event_line(attempt_event("usage", cost_usd=0.5))
        + b"no event\n"
        + event_line(attempt_event("error", text="Segmentation fault"))
    )
    out = io.StringIO()

    Follower(events, out, stopped=lambda: True).run()

    assert out.getvalue() == "Segmentation fault\n"


def test_an_old_attempt_renders_from_its_raw_stream_which_it_normalizes_once_and_leaves_as_written(
    tmp_path: Path,
) -> None:
    attempt = old_attempt(tmp_path)

    first = render(attempt / EVENTS)
    (attempt / "stream.ndjson").unlink()
    again = render(attempt / EVENTS)

    assert first == again == rendered_fixture()
    assert all(read_event(line) for line in (attempt / EVENTS).read_bytes().splitlines())
    attempt = old_attempt(tmp_path / "again")
    render(attempt / EVENTS)
    assert (attempt / "stream.ndjson").read_bytes() == FIXTURE.read_bytes()


def test_attempt_terminal_log_follows_the_agent_attach_prints_it_and_the_raw_stream_stays_raw(
    factory: Harness,
) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})

    result = factory.run(spec)

    assert result.code == 0, result.out
    run_id = run_id_of(result.out)
    attempt_dir = factory.executor.requests[0].attempt_dir
    log = ANSI.sub("", (attempt_dir / "terminal.log").read_text())
    assert "Done." in log.splitlines()
    assert "rate limit allowed" in log
    assert any(line.startswith("result success") for line in log.splitlines())
    raw = (attempt_dir / "stream.ndjson").read_text().splitlines()
    assert raw[0].startswith('{"type": "system", "subtype": "init"')
    assert raw[-1].startswith('{"type": "result"')
    events = [read_event(line) for line in (attempt_dir / EVENTS).read_bytes().splitlines()]
    assert [event["type"] for event in events if event][-1] == "result"

    attached = factory.cli("attach", run_id, "t1")
    assert attached.code == 0, attached.err
    assert "Done." in ANSI.sub("", attached.out).splitlines()
    assert "no Attempt" in factory.cli("attach", run_id, "nope").err
    assert "no Run" in factory.cli("attach", "nope", "t1").err
