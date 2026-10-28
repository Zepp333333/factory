"""Commands are the requests that change a Run, which any process puts in the Journal's inbox.

One type stands for each kind of Command and is built and parsed here, and one submit-and-wait serves the CLI.
The Orchestrator of the Run accepts or rejects a Command on a tick.
"""

from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, Literal

from factory.clock import Clock
from factory.custody import RunCustody
from factory.journal import CommandRow, Journal, WaitingRow
from factory.states import CommandState, RunState

# `decide` names this in place of a Ticket to answer the Decision of the Run itself.
RUN_LEVEL = "@run"

# The CLI looks this often, in seconds, for the Orchestrator's mark on its Command.
POLL_SECONDS = 0.1


class BadCommand(ValueError):
    """A BadCommand is a row in the inbox that no Command type can be parsed from."""


@dataclass(frozen=True)
class Decide:
    """An answer to the Decision of `ticket`, or of the Run when `ticket` is RUN_LEVEL.

    The Command names the Decision `node` and `round` it answers, so an answer that comes after another one is
    rejected as already decided even when the Ticket waits at a later Decision by then.
    """

    kind: ClassVar[str] = "decide"
    noun: ClassVar[str] = "answer"
    ticket: str
    outcome: str
    reasons: list[str]
    source: str
    node: str | None
    round: int | None


@dataclass(frozen=True)
class Cancel:
    """A request to abandon `ticket`."""

    kind: ClassVar[str] = "cancel"
    noun: ClassVar[str] = "cancel"
    ticket: str


@dataclass(frozen=True)
class Override:
    """A new outcome for the current or last Decision of `ticket`."""

    kind: ClassVar[str] = "override"
    noun: ClassVar[str] = "override"
    ticket: str
    outcome: str
    reasons: list[str]


@dataclass(frozen=True)
class Limits:
    """New parallelism and round limits of the Run, where `None` and an empty dict leave a limit alone."""

    kind: ClassVar[str] = "limits"
    noun: ClassVar[str] = "limits"
    max_parallel: int | None = None
    rounds: dict[str, int] = field(default_factory=dict)


Command = Decide | Cancel | Override | Limits

KINDS: dict[str, type[Command]] = {cls.kind: cls for cls in (Decide, Cancel, Override, Limits)}


def decide_for(ticket: str, outcome: str, reasons: list[str], source: str, waiting: WaitingRow | None) -> Decide:
    """Builds the answer to the Decision `waiting` of `ticket`, or one that names none when nothing waits."""
    return Decide(
        ticket, outcome, reasons, source, waiting.node if waiting else None, waiting.round if waiting else None
    )


def payload_of(command: Command) -> dict[str, Any]:
    """Returns the inbox payload of a Command."""
    return {f.name: getattr(command, f.name) for f in fields(command)}


def parse(row: CommandRow) -> Command:
    """Returns the typed Command of an inbox row, or raises BadCommand with the reason."""
    cls = KINDS.get(row.kind)
    if cls is None:
        raise BadCommand(f"unknown command {row.kind}")
    try:
        return cls(**row.payload)
    except TypeError as error:
        raise BadCommand(f"malformed {row.kind}: {error}") from None


def submit(journal: Journal, run_id: str, command: Command) -> int:
    """Puts the Command in the inbox of the Run and returns its id."""
    return journal.submit_command(run_id, command.kind, payload_of(command))


@dataclass(frozen=True)
class Sent:
    """A Sent is how a Command ended for the CLI that waited for it.

    `accepted` and `rejected` come from the Run's Orchestrator with its `text`.
    `waits` means the Run is orphaned, so the Command waits in the inbox for `factory resume`.
    `refused` means no Command could be sent or the Run ended before it took it.
    """

    status: Literal["accepted", "rejected", "waits", "refused"]
    text: str


def send(
    journal: Journal,
    custody: RunCustody,
    clock: Clock,
    run_id: str,
    command: Command,
) -> Sent:
    """Puts the Command in the Run's inbox and waits until its Orchestrator accepts or rejects it.

    A Run counts as live only if its heartbeat is fresh, so a Run whose process died is orphaned here,
    and the Command waits in its inbox for `factory resume`, which applies it on its first tick.
    The Command stays in the inbox if the wait is interrupted, and the Run applies it on its next tick.
    """
    run = journal.run(run_id)
    if run is None:
        return Sent("refused", f"no Run {run_id}")
    custody.is_live(run_id)
    run = journal.run(run_id)
    assert run is not None
    if run.state not in (RunState.RUNNING, RunState.ORPHANED):
        return Sent("refused", f"Run {run.id} is {run.state}, and takes no more Commands")
    command_id = submit(journal, run_id, command)
    while (row := journal.command(command_id)).state == CommandState.PENDING:
        custody.is_live(run_id)
        current = journal.run(run_id)
        if current is None or current.state == RunState.ORPHANED:
            return Sent(
                "waits",
                f"Run {run_id} is orphaned; the {command.noun} waits in its inbox for `factory resume {run_id}`",
            )
        if current.state != RunState.RUNNING:
            return Sent("refused", f"Run {run_id} ended before it took the {command.noun}")
        clock.sleep(POLL_SECONDS)
    if row.state == CommandState.REJECTED:
        return Sent("rejected", f"{command.noun} rejected: {row.result}")
    return Sent("accepted", row.result or "")
