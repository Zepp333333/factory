import re
import subprocess
import sys
from pathlib import Path

from harness import AgentScript, Harness, run_id_of

FIXTURE = Path(__file__).parent / "fixtures" / "streams" / "session.ndjson"
TICKET = "---\ntitle: Greet\n---\nWrite hello.txt.\n"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def render(stream: Path) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-m", "factory.render", str(stream)], check=True, capture_output=True, text=True, timeout=30
    ).stdout
    return ANSI.sub("", out).splitlines()


def test_renderer_shows_text_tool_calls_short_results_rate_limits_and_the_result() -> None:
    lines = render(FIXTURE)

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
    lines = render(FIXTURE)

    assert [line for line in lines if line.startswith("thinking")] == ["thinking: Weighing the options"]


def test_renderer_leaves_the_stream_as_the_agent_wrote_it(tmp_path: Path) -> None:
    stream = tmp_path / "stream.ndjson"
    stream.write_bytes(FIXTURE.read_bytes())

    render(stream)

    assert stream.read_bytes() == FIXTURE.read_bytes()


def test_attempt_terminal_log_follows_the_agent_and_the_raw_stream_stays_raw(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})

    result = factory.run(spec)

    assert result.code == 0, result.out
    attempt_dir = factory.executor.requests[0].attempt_dir
    log = ANSI.sub("", (attempt_dir / "terminal.log").read_text())
    assert "Done." in log.splitlines()
    assert "rate limit allowed" in log
    assert any(line.startswith("result success") for line in log.splitlines())
    raw = (attempt_dir / "stream.ndjson").read_text().splitlines()
    assert raw[0].startswith('{"type": "system", "subtype": "init"')
    assert raw[-1].startswith('{"type": "result"')


def test_attach_prints_the_terminal_of_the_latest_attempt_of_a_ticket(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})
    run_id = run_id_of(factory.run(spec).out)

    result = factory.cli("attach", run_id, "t1")

    assert result.code == 0, result.err
    assert "Done." in ANSI.sub("", result.out).splitlines()


def test_attach_to_a_ticket_without_attempts_or_a_missing_run_fails(factory: Harness) -> None:
    spec = factory.write_spec("Greeting", "Say hello.", {"t1": TICKET})
    factory.executor.scripts["implement"] = AgentScript(files={"hello.txt": "hello\n"}, output={"handoff": "done"})
    run_id = run_id_of(factory.run(spec).out)

    assert "no Attempt" in factory.cli("attach", run_id, "nope").err
    assert "no Run" in factory.cli("attach", "nope", "t1").err
