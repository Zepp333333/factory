"""The core reads no harness format: the Executor tells progress and renders its own stream.

These tests run an Attempt over a fake Executor whose stream is plain text lines, not Claude's `stream-json`.
"""

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from factory.attempt import Attempts, Ports
from factory.events import Event
from factory.executor import AttemptRequest, AttemptResult
from factory.live import Silence
from factory.pipeline import Stage
from factory.render import Follower
from factory.sandbox import Sandbox
from factory.workbench import AttemptTerminal
from test_attempt_charge import FakeExecutor, ScriptedHandle, World, world  # noqa: F401


@dataclass
class TextExecutor(FakeExecutor):
    """TextExecutor writes lines like `say: hi`, where `say:` is progress and `beat:` is a heartbeat."""

    name = "text"
    stream_lines: list[str] = field(default_factory=list)

    def is_progress(self, line: bytes) -> bool:
        return line.startswith(b"say:")

    def render_line(self, line: str) -> tuple[str | None, bool]:
        if line.startswith("beat:"):
            return None, False
        return line.upper(), line == "end"

    def start(self, request: AttemptRequest, sandbox: Sandbox) -> ScriptedHandle:
        request.stream.write_text("".join(f"{line}\n" for line in self.stream_lines))
        return super().start(request, sandbox)  # type: ignore[return-value]


class TextWorkbench:
    """TextWorkbench renders in this process what the named Executor's stream shows."""

    def __init__(self, executor: TextExecutor) -> None:
        self.executor = executor
        self.opened: list[str] = []
        self.logs: list[Path] = []

    def open_attempt(self, stream: Path, log: Path, workspace: Path, title: str, executor: str) -> AttemptTerminal:
        self.opened.append(executor)
        self.logs.append(log)
        workbench = self

        class Terminal:
            def alive(self) -> bool:
                return True

            def close(self) -> None:
                with log.open("w") as out:
                    Follower(stream, workbench.executor, out, stopped=lambda: True).run()

        return Terminal()

    def create_workspace(self, repo: Path, path: Path, branch: str) -> None:
        pass

    def remove_workspace(self, repo: Path, path: Path) -> None:
        pass

    def has_workspace(self, repo: Path, path: Path) -> bool:
        return True

    def mark_stage(self, path: Path, text: str) -> None:
        pass


def attempts_over(world: World, executor: TextExecutor, workbench: TextWorkbench) -> Attempts:  # noqa: F811
    ports = world.attempts.ports
    world.attempts.ports = Ports(executor, ports.sandbox, workbench, ports.clock)
    return world.attempts


def stalling(attempts: Attempts, stall: float) -> Stage:
    """Gives the Pipeline's one Stage a `stall` and a timeout far past it, and returns the Stage."""
    node = attempts.pipeline.nodes["implement"]
    assert isinstance(node, Stage)
    node = dataclasses.replace(node, stall=stall, timeout=3600)
    attempts.pipeline.nodes["implement"] = node
    return node


def test_silence_counts_only_what_the_executor_calls_progress(tmp_path: Path) -> None:
    stream = tmp_path / "stream.ndjson"
    stream.write_text("beat: 1\n")
    start = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    silence = Silence(stream, TextExecutor(), start)

    assert silence.watch(start + timedelta(seconds=30)) == 30
    with stream.open("a") as out:
        out.write("say: hi\n")
    assert silence.watch(start + timedelta(seconds=60)) == 0


def test_an_attempt_over_a_non_claude_stream_stalls_on_heartbeats_alone(world: World) -> None:  # noqa: F811
    executor = TextExecutor(stream_lines=["beat: 1"])
    attempts = attempts_over(world, executor, TextWorkbench(executor))
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


def test_an_attempt_over_a_non_claude_stream_stays_alive_while_the_executor_sees_progress(world: World) -> None:  # noqa: F811
    executor = TextExecutor(stream_lines=["say: one"])
    attempts = attempts_over(world, executor, TextWorkbench(executor))
    node = stalling(attempts, 60)
    ticket = world.ticket()
    assert attempts.start(ticket, node)
    world.clock.sleep(50)

    assert attempts.step(ticket, node, lambda: False) is None
    with Path(ticket.running.request.stream).open("a") as out:  # type: ignore[union-attr]
        out.write("say: two\n")
    world.clock.sleep(50)
    assert attempts.step(ticket, node, lambda: False) is None


def test_the_terminal_of_an_attempt_shows_what_the_executor_renders(world: World) -> None:  # noqa: F811
    executor = TextExecutor(stream_lines=["say: hello", "beat: 1", "end"])
    executor.result = AttemptResult(output={"handoff": "done"}, failure=None)
    workbench = TextWorkbench(executor)
    attempts = attempts_over(world, executor, workbench)
    node = attempts.pipeline.nodes["implement"]
    assert isinstance(node, Stage)
    ticket = world.ticket()
    assert attempts.start(ticket, node)

    attempts.step(ticket, node, lambda: False)

    assert workbench.opened == ["text"]
    assert workbench.logs[0].read_text().splitlines() == ["SAY: HELLO", "END"]
