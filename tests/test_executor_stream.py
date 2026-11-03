"""The core reads no harness format: it reads only the Attempt events an Executor writes next to its raw stream.

These tests run an Attempt over a fake Executor whose raw stream is plain text lines, not Claude's `stream-json`,
and which writes `say:` as a message with progress and `beat:` as a heartbeat that is none.
"""

import dataclasses
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from factory.core.events import Event
from factory.pipeline.pipeline import Stage
from factory.ports.executor import AttemptEvent, AttemptRequest, AttemptResult, attempt_event, event_line
from factory.ports.run_ports import Ports
from factory.ports.sandbox import Sandbox
from factory.ports.workbench import AttemptTerminal
from factory.render import Follower
from factory.run.attempt import Attempts
from factory.run.live import Silence
from test_attempt_charge import FakeExecutor, FakeWorkbench, ScriptedHandle, World, world  # noqa: F401

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def events_of(line: str) -> list[AttemptEvent]:
    """Returns the Attempt events of one line of the text stream."""
    if line.startswith("say:"):
        return [attempt_event("progress"), attempt_event("text", kind="message", text=line.upper())]
    if line == "end":
        return [attempt_event("result", status="success", text="END")]
    return []


def write_events(events: Path, lines: list[str]) -> None:
    with events.open("ab") as out:
        out.write(b"".join(event_line(event) for line in lines for event in events_of(line)))


@dataclass
class TextExecutor(FakeExecutor):
    """TextExecutor writes its raw stream as lines like `say: hi` and its Attempt events by `events_of`."""

    name = "text"
    stream_lines: list[str] = field(default_factory=list)

    def start(self, request: AttemptRequest, sandbox: Sandbox) -> ScriptedHandle:
        request.stream.write_text("".join(f"{line}\n" for line in self.stream_lines))
        write_events(request.events, self.stream_lines)
        return super().start(request, sandbox)  # type: ignore[return-value]


class TextWorkbench(FakeWorkbench):
    """TextWorkbench renders in this process what the Attempt events show."""

    def __init__(self) -> None:
        self.logs: list[Path] = []

    def open_attempt(self, events: Path, log: Path, workspace: Path, title: str) -> AttemptTerminal:
        self.logs.append(log)

        class Terminal:
            def alive(self) -> bool:
                return True

            def close(self) -> None:
                with log.open("w") as out:
                    Follower(events, out, stopped=lambda: True).run()

        return Terminal()


def attempts_over(world: World, executor: TextExecutor, workbench: TextWorkbench) -> Attempts:  # noqa: F811
    ports = world.attempts.ports
    world.attempts.ports = Ports(executor, ports.sandbox, workbench, ports.clock, ports.kit)
    return world.attempts


def stalling(attempts: Attempts, stall: float) -> Stage:
    """Gives the Pipeline's one Stage a `stall` and a timeout far past it, and returns the Stage."""
    node = attempts.pipeline.nodes["implement"]
    assert isinstance(node, Stage)
    node = dataclasses.replace(node, settings=dataclasses.replace(node.settings, stall=stall, timeout=3600))
    attempts.pipeline.nodes["implement"] = node
    return node


def test_silence_counts_only_progress_events(tmp_path: Path) -> None:
    events = tmp_path / "events.ndjson"
    events.write_bytes(event_line(attempt_event("windows", status="allowed")) + b"no event\n")
    start = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    silence = Silence(events, start)

    assert silence.watch(start + timedelta(seconds=30)) == 30
    write_events(events, ["say: hi"])
    assert silence.watch(start + timedelta(seconds=60)) == 0


def test_an_attempt_over_a_non_claude_stream_stalls_on_heartbeats_alone(world: World) -> None:  # noqa: F811
    executor = TextExecutor(stream_lines=["beat: 1"])
    attempts = attempts_over(world, executor, TextWorkbench())
    node = stalling(attempts, 60)
    ticket = world.ticket()
    assert attempts.start(ticket, node)
    assert attempts.step(ticket, node, lambda: False) is None
    world.clock.sleep(61)

    ended = attempts.step(ticket, node, lambda: False)

    assert ended is not None and ended.outcome is None
    assert executor.handles[0].killed
    [(_, payload)] = [e for e in world.events if e[0] == Event.AttemptFinished]
    assert payload["cause"] == "stalled"


def test_an_attempt_over_a_non_claude_stream_stays_alive_while_its_events_show_progress(world: World) -> None:  # noqa: F811
    executor = TextExecutor(stream_lines=["say: one"])
    attempts = attempts_over(world, executor, TextWorkbench())
    node = stalling(attempts, 60)
    ticket = world.ticket()
    assert attempts.start(ticket, node)
    world.clock.sleep(50)

    assert attempts.step(ticket, node, lambda: False) is None
    write_events(ticket.running.request.events, ["say: two"])  # type: ignore[union-attr]
    world.clock.sleep(50)
    assert attempts.step(ticket, node, lambda: False) is None


def test_the_terminal_of_an_attempt_shows_what_its_events_render(world: World) -> None:  # noqa: F811
    executor = TextExecutor(stream_lines=["say: hello", "beat: 1", "end", "say: after"])
    executor.result = AttemptResult(output={"handoff": "done"}, failure=None)
    workbench = TextWorkbench()
    attempts = attempts_over(world, executor, workbench)
    node = attempts.pipeline.nodes["implement"]
    assert isinstance(node, Stage)
    ticket = world.ticket()
    assert attempts.start(ticket, node)

    attempts.step(ticket, node, lambda: False)

    shown = ANSI.sub("", workbench.logs[0].read_text()).splitlines()
    assert shown == ["SAY: HELLO", "result success turns None None ms", "END"]
