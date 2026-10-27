"""The Journal is one SQLite event log per machine, with projections kept in the same transaction.

`append` is the only write path for events.
It stores the event and applies it to the projection tables before the transaction commits,
so the projections can always be rebuilt from the events.
A `transaction` makes several appends and the taking of a slot commit together or not at all.
A Run beats with `Heartbeat` events, and a Run whose beat went stale is orphaned by `RunOrphaned`.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from factory.clock import Clock
from factory.pipeline import TERMINAL_NODES
from factory.states import SANDBOX_FAILURES, CommandState, RunState, TicketState

# The Describer holds a slot under this name, which no Ticket id can be, because an id has no "/".
DESCRIBER_SLOT = "/describer"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    spec_ref TEXT NOT NULL,
    integration_branch TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    heartbeat_at TEXT
);
CREATE TABLE IF NOT EXISTS pauses (
    run_id TEXT PRIMARY KEY,
    resets_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots (
    run_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    PRIMARY KEY (run_id, ticket_id)
);
CREATE TABLE IF NOT EXISTS tickets (
    run_id TEXT NOT NULL,
    id TEXT NOT NULL,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    node TEXT,
    last_stage TEXT,
    round INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    sandbox_losses INTEGER NOT NULL DEFAULT 0,
    kept_ref TEXT,
    last_loss TEXT,
    PRIMARY KEY (run_id, id)
);
CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    result TEXT
);
CREATE TABLE IF NOT EXISTS waiting (
    run_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    node TEXT NOT NULL,
    round INTEGER NOT NULL,
    since TEXT NOT NULL,
    card TEXT NOT NULL,
    outcomes TEXT NOT NULL,
    PRIMARY KEY (run_id, ticket_id)
);
CREATE TABLE IF NOT EXISTS attempts (
    run_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    node TEXT NOT NULL,
    round INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    state TEXT NOT NULL,
    cause TEXT,
    PRIMARY KEY (run_id, ticket_id, node, round, attempt)
);
"""

Projector = Callable[[sqlite3.Connection, str, str, dict[str, Any]], None]


@dataclass(frozen=True)
class RunRow:
    id: str
    state: str
    spec_ref: str
    integration_branch: str
    paused_until: str | None


@dataclass(frozen=True)
class TicketRow:
    id: str
    title: str
    state: str
    node: str | None
    last_stage: str | None
    round: int
    attempt: int
    sandbox_losses: int
    kept_ref: str | None
    last_loss: str | None


@dataclass(frozen=True)
class EventRow:
    at: datetime
    type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class WaitingRow:
    """A WaitingRow is a Decision that waits for a human answer, with the card that asks the question."""

    ticket: str
    node: str
    round: int
    since: str
    card: str
    outcomes: list[str]


@dataclass(frozen=True)
class CommandRow:
    """A CommandRow is a Command in the inbox, `pending` until the Orchestrator accepts or rejects it."""

    id: int
    kind: str
    payload: dict[str, Any]
    state: CommandState
    result: str | None


class Journal:
    def __init__(self, path: Path, clock: Clock) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.executescript(SCHEMA)
        if "heartbeat_at" not in [row[1] for row in self._db.execute("PRAGMA table_info(runs)")]:
            self._db.execute("ALTER TABLE runs ADD COLUMN heartbeat_at TEXT")
        columns = [row[1] for row in self._db.execute("PRAGMA table_info(tickets)")]
        if "sandbox_losses" not in columns:
            self._db.execute("ALTER TABLE tickets ADD COLUMN sandbox_losses INTEGER NOT NULL DEFAULT 0")
        if "kept_ref" not in columns:
            self._db.execute("ALTER TABLE tickets ADD COLUMN kept_ref TEXT")
        if "last_loss" not in columns:
            self._db.execute("ALTER TABLE tickets ADD COLUMN last_loss TEXT")
        self._clock = clock
        self._in_transaction = False

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Holds the write lock of the whole machine's Journal, so what is appended inside commits as one."""
        assert not self._in_transaction, "transactions do not nest"
        self._db.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")
        finally:
            self._in_transaction = False

    def take_slot(self, run_id: str, ticket_id: str, cap: int) -> bool:
        """Takes a subscription slot for an Attempt, or returns False when the machine's `cap` slots are taken.

        `ticket_id` is the Ticket's id, or DESCRIBER_SLOT for an Attempt of the Describer.
        The `AttemptFinished` of the Ticket, the end of the Describer's Attempt or the `RunFinished` of the Run
        releases the slot.
        """
        assert self._in_transaction, "a slot is taken in the transaction of the Attempt that uses it"
        (taken,) = self._db.execute("SELECT COUNT(*) FROM slots").fetchone()
        if taken >= cap:
            return False
        self._db.execute("INSERT INTO slots (run_id, ticket_id) VALUES (?, ?)", (run_id, ticket_id))
        return True

    def submit_command(self, run_id: str, kind: str, payload: dict[str, Any]) -> int:
        """Puts a Command in the inbox of a Run and returns its id.

        This is no event: any process may put a Command in, and only the Run's Orchestrator accepts or rejects it,
        by journaling `CommandAccepted` or `CommandRejected`.
        """
        with self.transaction():
            cursor = self._db.execute(
                "INSERT INTO commands (run_id, kind, payload) VALUES (?, ?, ?)", (run_id, kind, json.dumps(payload))
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def pending_commands(self, run_id: str) -> list[CommandRow]:
        rows = self._db.execute(
            "SELECT id, kind, payload, state, result FROM commands WHERE run_id = ? AND state = ? ORDER BY id",
            (run_id, CommandState.PENDING),
        )
        return [_command_row(row) for row in rows]

    def command(self, command_id: int) -> CommandRow:
        row = self._db.execute(
            "SELECT id, kind, payload, state, result FROM commands WHERE id = ?", (command_id,)
        ).fetchone()
        return _command_row(row)

    def waiting(self, run_id: str) -> list[WaitingRow]:
        """Returns the Decisions of a Run that wait for a human, the longest waiting first."""
        rows = self._db.execute(
            "SELECT ticket_id, node, round, since, card, outcomes FROM waiting WHERE run_id = ? ORDER BY since, rowid",
            (run_id,),
        )
        return [WaitingRow(t, n, r, since, card, json.loads(o)) for t, n, r, since, card, o in rows]

    def append(self, run_id: str, type: str, payload: dict[str, Any]) -> int:
        if self._in_transaction:
            return self._store(run_id, type, payload)
        with self.transaction():
            return self._store(run_id, type, payload)

    def _store(self, run_id: str, type: str, payload: dict[str, Any]) -> int:
        at = self._clock.now().isoformat()
        cursor = self._db.execute(
            "INSERT INTO events (run_id, at, type, payload) VALUES (?, ?, ?, ?)",
            (run_id, at, type, json.dumps(payload)),
        )
        PROJECTORS.get(type, _ignore)(self._db, run_id, at, payload)
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def orphan_stale(self, stale_seconds: float, keep: str | None = None) -> list[str]:
        """Marks every running Run but `keep` whose last heartbeat is older than `stale_seconds` as orphaned.

        Any Orchestrator and `factory resume` may do this, because the process that should have beaten is gone.
        The `RunOrphaned` event frees the Run's slots, and the Run stays so until `resume` takes it up.
        Returns the ids of the Runs it orphaned.
        """
        now = self._clock.now()
        orphaned = []
        with self.transaction():
            rows = self._db.execute(
                "SELECT id, COALESCE(heartbeat_at, started_at) FROM runs WHERE state = ? AND id != ?",
                (RunState.RUNNING, keep or ""),
            ).fetchall()
            for run_id, beat in rows:
                silent = (now - datetime.fromisoformat(beat)).total_seconds()
                if silent >= stale_seconds:
                    self._store(run_id, "RunOrphaned", {"last_heartbeat": beat, "silent_seconds": silent})
                    orphaned.append(run_id)
        return orphaned

    def run(self, run_id: str) -> RunRow | None:
        row = self._db.execute(
            "SELECT id, state, spec_ref, integration_branch, resets_at"
            " FROM runs LEFT JOIN pauses ON pauses.run_id = runs.id WHERE id = ?",
            (run_id,),
        ).fetchone()
        return RunRow(*row) if row else None

    def events(self, run_id: str) -> list[EventRow]:
        """Returns the events of a Run in the order they were journaled."""
        rows = self._db.execute("SELECT at, type, payload FROM events WHERE run_id = ? ORDER BY seq", (run_id,))
        return [EventRow(datetime.fromisoformat(at), type, json.loads(payload)) for at, type, payload in rows]

    def tickets(self, run_id: str) -> list[TicketRow]:
        rows = self._db.execute(
            "SELECT id, title, state, node, last_stage, round, attempt, sandbox_losses, kept_ref, last_loss"
            " FROM tickets WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        return [TicketRow(*row) for row in rows]


def _ignore(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    pass


def _run_started(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute(
        "INSERT INTO runs (id, state, spec_ref, integration_branch, started_at, heartbeat_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, RunState.RUNNING, payload["spec_ref"], payload["integration_branch"], at, at),
    )


def _heartbeat(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ?", (at, run_id))


def _run_orphaned(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("UPDATE runs SET state = ? WHERE id = ?", (RunState.ORPHANED, run_id))
    db.execute("DELETE FROM slots WHERE run_id = ?", (run_id,))


def _run_reclaimed(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("UPDATE runs SET state = ?, heartbeat_at = ? WHERE id = ?", (RunState.RUNNING, at, run_id))


def _spec_snapshotted(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.executemany(
        "INSERT INTO tickets (run_id, id, title, state) VALUES (?, ?, ?, ?)",
        [(run_id, t["id"], t["title"], TicketState.QUEUED) for t in payload["snapshot"]["tickets"]],
    )


def _ticket_claimed(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute(
        "UPDATE tickets SET state = ? WHERE run_id = ? AND id = ?", (TicketState.ACTIVE, run_id, payload["ticket"])
    )


def _node_entered(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute(
        "UPDATE tickets SET node = ? WHERE run_id = ? AND id = ?",
        (payload["node"], run_id, payload["ticket"]),
    )
    if "round" in payload:
        db.execute(
            "UPDATE tickets SET last_stage = ?, round = ?, attempt = 0 WHERE run_id = ? AND id = ?",
            (payload["node"], payload["round"], run_id, payload["ticket"]),
        )
    if payload["node"] in TERMINAL_NODES:
        db.execute(
            "UPDATE tickets SET state = ? WHERE run_id = ? AND id = ? AND state = ?",
            (payload["node"], run_id, payload["ticket"], TicketState.ACTIVE),
        )


def _attempt_started(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    key = (run_id, payload["ticket"], payload["node"], payload["round"], payload["attempt"])
    db.execute(
        "INSERT INTO attempts (run_id, ticket_id, node, round, attempt, state) VALUES (?, ?, ?, ?, ?, ?)",
        (*key, "running"),
    )
    db.execute(
        "UPDATE tickets SET attempt = ? WHERE run_id = ? AND id = ?",
        (payload["attempt"], run_id, payload["ticket"]),
    )


def _sandbox_lost(db: sqlite3.Connection, run_id: str, ticket: str, loss: str) -> None:
    db.execute(
        "UPDATE tickets SET sandbox_losses = sandbox_losses + 1, last_loss = ? WHERE run_id = ? AND id = ?",
        (loss, run_id, ticket),
    )


def _probe_lost(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    _sandbox_lost(db, run_id, payload["ticket"], f"probe of {payload['node']}: {payload['reason']}")


def _abandoned_branch_saved(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute(
        "UPDATE tickets SET kept_ref = ? WHERE run_id = ? AND id = ?", (payload["ref"], run_id, payload["ticket"])
    )


def _attempt_finished(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("DELETE FROM slots WHERE run_id = ? AND ticket_id = ?", (run_id, payload["ticket"]))
    if payload.get("cause") in SANDBOX_FAILURES:
        loss = f"{payload['cause']} in {payload['node']}"
        _sandbox_lost(db, run_id, payload["ticket"], f"{loss}: {payload['reason']}" if payload.get("reason") else loss)
    db.execute(
        "UPDATE attempts SET state = ?, cause = ?"
        " WHERE run_id = ? AND ticket_id = ? AND node = ? AND round = ? AND attempt = ?",
        (
            payload["outcome"],
            payload.get("cause"),
            run_id,
            payload["ticket"],
            payload["node"],
            payload["round"],
            payload["attempt"],
        ),
    )


def _attempt_lost(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("DELETE FROM slots WHERE run_id = ? AND ticket_id = ?", (run_id, payload["ticket"]))
    if payload["cause"] in SANDBOX_FAILURES:
        _sandbox_lost(db, run_id, payload["ticket"], f"{payload['cause']} in {payload['node']}: {payload['reason']}")
    db.execute(
        "UPDATE attempts SET state = ?, cause = ?"
        " WHERE run_id = ? AND ticket_id = ? AND node = ? AND round = ? AND attempt = ?",
        (
            "lost",
            payload["cause"],
            run_id,
            payload["ticket"],
            payload["node"],
            payload["round"],
            payload["attempt"],
        ),
    )


def _operation_finished(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    if payload["kind"] == "merge" and payload["outcome"] == "merged":
        db.execute(
            "UPDATE tickets SET state = ? WHERE run_id = ? AND id = ?", (TicketState.MERGED, run_id, payload["ticket"])
        )


def _command_row(row: tuple[Any, ...]) -> CommandRow:
    id, kind, payload, state, result = row
    return CommandRow(id, kind, json.loads(payload), CommandState(state), result)


def _decision_waiting(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute(
        "INSERT OR REPLACE INTO waiting (run_id, ticket_id, node, round, since, card, outcomes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            payload["ticket"],
            payload["node"],
            payload["round"],
            at,
            payload["card"],
            json.dumps(payload["outcomes"]),
        ),
    )


def _decision_decided(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("DELETE FROM waiting WHERE run_id = ? AND ticket_id = ?", (run_id, payload["ticket"]))


def _ticket_cancelled(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("DELETE FROM waiting WHERE run_id = ? AND ticket_id = ?", (run_id, payload["ticket"]))
    db.execute(
        "UPDATE tickets SET state = ? WHERE run_id = ? AND id = ? AND state = ?",
        (TicketState.ABANDONED, run_id, payload["ticket"], TicketState.QUEUED),
    )


def _command_accepted(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute(
        "UPDATE commands SET state = ?, result = ? WHERE id = ?",
        (CommandState.ACCEPTED, payload["result"], payload["command"]),
    )


def _command_rejected(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute(
        "UPDATE commands SET state = ?, result = ? WHERE id = ?",
        (CommandState.REJECTED, payload["reason"], payload["command"]),
    )


def _run_paused(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("INSERT OR REPLACE INTO pauses (run_id, resets_at) VALUES (?, ?)", (run_id, payload["resets_at"]))


def _run_resumed(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("DELETE FROM pauses WHERE run_id = ?", (run_id,))


def _describer_finished(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("DELETE FROM slots WHERE run_id = ? AND ticket_id = ?", (run_id, DESCRIBER_SLOT))


def _run_finished(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("UPDATE runs SET state = ?, finished_at = ? WHERE id = ?", (payload["state"], at, run_id))
    db.execute("DELETE FROM pauses WHERE run_id = ?", (run_id,))
    db.execute("DELETE FROM slots WHERE run_id = ?", (run_id,))
    db.execute("DELETE FROM waiting WHERE run_id = ?", (run_id,))


PROJECTORS: dict[str, Projector] = {
    "RunStarted": _run_started,
    "SpecSnapshotted": _spec_snapshotted,
    "TicketClaimed": _ticket_claimed,
    "NodeEntered": _node_entered,
    "AttemptStarted": _attempt_started,
    "AttemptFinished": _attempt_finished,
    "AttemptLost": _attempt_lost,
    "Heartbeat": _heartbeat,
    "RunOrphaned": _run_orphaned,
    "RunReclaimed": _run_reclaimed,
    "ProbeLost": _probe_lost,
    "AbandonedBranchSaved": _abandoned_branch_saved,
    "OperationFinished": _operation_finished,
    "DecisionWaiting": _decision_waiting,
    "DecisionDecided": _decision_decided,
    "DecisionOverridden": _decision_decided,
    "TicketCancelled": _ticket_cancelled,
    "CommandAccepted": _command_accepted,
    "CommandRejected": _command_rejected,
    "RunPaused": _run_paused,
    "RunResumed": _run_resumed,
    "RunFinished": _run_finished,
    "PullRequestDescribed": _describer_finished,
    "DescriberFailed": _describer_finished,
}
