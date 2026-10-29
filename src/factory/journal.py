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
from factory.events import Event
from factory.pipeline import TERMINAL_NODES
from factory.states import SANDBOX_FAILURES, SANDBOX_LOSSES, CommandState, RunState, TicketState

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
    claimed_at TEXT,
    entered_at TEXT,
    finished_at TEXT,
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
CREATE TABLE IF NOT EXISTS tracker_cursor (
    run_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    run_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    node TEXT NOT NULL,
    round INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    state TEXT NOT NULL,
    cause TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration REAL,
    cost_usd REAL,
    turns INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    PRIMARY KEY (run_id, ticket_id, node, round, attempt)
);
"""

ATTEMPT_USAGE_COLUMNS = {
    "started_at": "TEXT",
    "finished_at": "TEXT",
    "duration": "REAL",
    "cost_usd": "REAL",
    "turns": "INTEGER",
    "input_tokens": "INTEGER",
    "output_tokens": "INTEGER",
    "cache_read_tokens": "INTEGER",
    "cache_write_tokens": "INTEGER",
}


def _spend_sql(where: str) -> str:
    """Returns two subqueries: the cost of the Attempts that ended, and how many of them did not say what they cost."""
    ended = f"FROM attempts WHERE state != 'running' AND {where}"
    return f"(SELECT COALESCE(SUM(cost_usd), 0) {ended}), (SELECT COALESCE(SUM(cost_usd IS NULL), 0) {ended})"


Projector = Callable[[sqlite3.Connection, str, str, dict[str, Any]], None]


@dataclass(frozen=True)
class Spend:
    """A Spend is the cost in USD of the finished Attempts of a Run or a Ticket.

    `unknown` counts the finished Attempts that gave no usage, so the cost is a lower bound when it is above zero.
    """

    cost_usd: float
    unknown: int


@dataclass(frozen=True)
class RunRow:
    id: str
    state: str
    spec_ref: str
    integration_branch: str
    paused_until: str | None
    started_at: datetime
    finished_at: datetime | None
    spend: Spend


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
    claimed_at: datetime | None
    entered_at: datetime | None
    finished_at: datetime | None
    spend: Spend


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
        for column in ("claimed_at", "entered_at", "finished_at"):
            if column not in columns:
                self._db.execute(f"ALTER TABLE tickets ADD COLUMN {column} TEXT")
        columns = [row[1] for row in self._db.execute("PRAGMA table_info(attempts)")]
        for column, kind in ATTEMPT_USAGE_COLUMNS.items():
            if column not in columns:
                self._db.execute(f"ALTER TABLE attempts ADD COLUMN {column} {kind}")
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
        The `RunOrphaned` event frees the Run's slots, and the Run stays so until `resume` or `finish` takes it up.
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
                    self._store(run_id, Event.RunOrphaned, {"last_heartbeat": beat, "silent_seconds": silent})
                    orphaned.append(run_id)
        return orphaned

    def run(self, run_id: str) -> RunRow | None:
        row = self._db.execute(
            "SELECT id, state, spec_ref, integration_branch, resets_at, started_at, finished_at"
            " FROM runs LEFT JOIN pauses ON pauses.run_id = runs.id WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        cost, unknown = self._db.execute(f"SELECT {_spend_sql('run_id = ?')}", (run_id, run_id)).fetchone()
        id, state, spec_ref, branch, resets_at, started, finished = row
        finished_at = datetime.fromisoformat(finished) if finished else None
        return RunRow(
            id, state, spec_ref, branch, resets_at, datetime.fromisoformat(started), finished_at, Spend(cost, unknown)
        )

    def run_ids(self) -> list[str]:
        """Returns the id of every Run in the Journal, oldest first."""
        return [row[0] for row in self._db.execute("SELECT id FROM runs ORDER BY started_at, id")]

    def tracker_cursor(self, run_id: str) -> int:
        """Returns the `seq` of the last event delivered to the Tracker, or 0 when none was."""
        row = self._db.execute("SELECT seq FROM tracker_cursor WHERE run_id = ?", (run_id,)).fetchone()
        return int(row[0]) if row else 0

    def events_after(self, run_id: str, seq: int) -> list[tuple[int, EventRow]]:
        """Returns the events of a Run journaled after `seq` with their `seq`, in order."""
        rows = self._db.execute(
            "SELECT seq, at, type, payload FROM events WHERE run_id = ? AND seq > ? AND type != ? ORDER BY seq",
            (run_id, seq, Event.Heartbeat),
        )
        return [(n, EventRow(datetime.fromisoformat(at), type, json.loads(payload))) for n, at, type, payload in rows]

    def events(self, run_id: str) -> list[EventRow]:
        """Returns the events of a Run in the order they were journaled."""
        rows = self._db.execute("SELECT at, type, payload FROM events WHERE run_id = ? ORDER BY seq", (run_id,))
        return [EventRow(datetime.fromisoformat(at), type, json.loads(payload)) for at, type, payload in rows]

    def tickets(self, run_id: str) -> list[TicketRow]:
        rows = self._db.execute(
            "SELECT id, title, state, node, last_stage, round, attempt, sandbox_losses, kept_ref, last_loss,"
            " claimed_at, entered_at, finished_at,"
            f" {_spend_sql('run_id = tickets.run_id AND ticket_id = tickets.id')}"
            " FROM tickets WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        return [_ticket_row(row) for row in rows]


def _ticket_row(row: tuple[Any, ...]) -> TicketRow:
    id, title, state, node, last_stage, round, attempt, losses, kept_ref, last_loss = row[:10]
    claimed, entered, finished = (datetime.fromisoformat(t) if t else None for t in row[10:13])
    spend = Spend(row[13], row[14])
    return TicketRow(
        id,
        title,
        state,
        node,
        last_stage,
        round,
        attempt,
        losses,
        kept_ref,
        last_loss,
        claimed,
        entered,
        finished,
        spend,
    )


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
        "UPDATE tickets SET state = ?, claimed_at = ? WHERE run_id = ? AND id = ?",
        (TicketState.ACTIVE, at, run_id, payload["ticket"]),
    )


def _node_entered(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute(
        "UPDATE tickets SET node = ?, entered_at = ? WHERE run_id = ? AND id = ?",
        (payload["node"], at, run_id, payload["ticket"]),
    )
    if "round" in payload:
        db.execute(
            "UPDATE tickets SET last_stage = ?, round = ?, attempt = 0 WHERE run_id = ? AND id = ?",
            (payload["node"], payload["round"], run_id, payload["ticket"]),
        )
    if payload["node"] in TERMINAL_NODES:
        db.execute(
            "UPDATE tickets SET finished_at = COALESCE(finished_at, ?) WHERE run_id = ? AND id = ?",
            (at, run_id, payload["ticket"]),
        )
        db.execute(
            "UPDATE tickets SET state = ? WHERE run_id = ? AND id = ? AND state IN (?, ?)",
            (payload["node"], run_id, payload["ticket"], TicketState.ACTIVE, TicketState.QUEUED),
        )


def _attempt_started(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    key = (run_id, payload["ticket"], payload["node"], payload["round"], payload["attempt"])
    db.execute(
        "INSERT INTO attempts (run_id, ticket_id, node, round, attempt, state, started_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (*key, "running", at),
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
    if payload.get("charge") == SANDBOX_LOSSES:
        loss = f"{payload['cause']} in {payload['node']}"
        _sandbox_lost(db, run_id, payload["ticket"], f"{loss}: {payload['reason']}" if payload.get("reason") else loss)
    usage = payload.get("usage") or {}
    db.execute(
        "UPDATE attempts SET state = ?, cause = ?, finished_at = ?, duration = ?, cost_usd = ?, turns = ?,"
        " input_tokens = ?, output_tokens = ?, cache_read_tokens = ?, cache_write_tokens = ?"
        " WHERE run_id = ? AND ticket_id = ? AND node = ? AND round = ? AND attempt = ?",
        (
            payload["outcome"],
            payload.get("cause"),
            at,
            payload.get("duration"),
            usage.get("cost_usd"),
            usage.get("turns"),
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cache_read_tokens"),
            usage.get("cache_write_tokens"),
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
    where = (run_id, payload["ticket"], payload["node"], payload["round"], payload["attempt"])
    started = db.execute(
        "SELECT started_at FROM attempts WHERE run_id = ? AND ticket_id = ? AND node = ? AND round = ? AND attempt = ?",
        where,
    ).fetchone()
    duration = (
        (datetime.fromisoformat(at) - datetime.fromisoformat(started[0])).total_seconds()
        if started and started[0]
        else None
    )
    db.execute(
        "UPDATE attempts SET state = ?, cause = ?, finished_at = ?, duration = ?"
        " WHERE run_id = ? AND ticket_id = ? AND node = ? AND round = ? AND attempt = ?",
        ("lost", payload["cause"], at, duration, *where),
    )


def _operation_finished(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    if payload["kind"] == "merge" and payload["outcome"] == "merged":
        db.execute(
            "UPDATE tickets SET state = ?, finished_at = ? WHERE run_id = ? AND id = ?",
            (TicketState.MERGED, at, run_id, payload["ticket"]),
        )


def _command_row(row: tuple[Any, ...]) -> CommandRow:
    id, kind, payload, state, result = row
    return CommandRow(id, kind, json.loads(payload), CommandState(state), result)


def _decision_waiting(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    for given in payload.get("gave_back", []):
        if given["counter"] == SANDBOX_LOSSES:
            db.execute(
                "UPDATE tickets SET sandbox_losses = MAX(0, sandbox_losses - 1) WHERE run_id = ? AND id = ?",
                (run_id, given["ticket"]),
            )
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
        "UPDATE tickets SET state = ?, finished_at = ? WHERE run_id = ? AND id = ? AND state = ?",
        (TicketState.ABANDONED, at, run_id, payload["ticket"], TicketState.QUEUED),
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


def _tracker_delivered(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("INSERT OR REPLACE INTO tracker_cursor (run_id, seq) VALUES (?, ?)", (run_id, payload["seq"]))


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
    Event.RunStarted: _run_started,
    Event.SpecSnapshotted: _spec_snapshotted,
    Event.TicketClaimed: _ticket_claimed,
    Event.NodeEntered: _node_entered,
    Event.AttemptStarted: _attempt_started,
    Event.AttemptFinished: _attempt_finished,
    Event.AttemptLost: _attempt_lost,
    Event.Heartbeat: _heartbeat,
    Event.RunOrphaned: _run_orphaned,
    Event.RunReclaimed: _run_reclaimed,
    Event.ProbeLost: _probe_lost,
    Event.AbandonedBranchSaved: _abandoned_branch_saved,
    Event.OperationFinished: _operation_finished,
    Event.DecisionWaiting: _decision_waiting,
    Event.DecisionDecided: _decision_decided,
    Event.DecisionOverridden: _decision_decided,
    Event.TicketCancelled: _ticket_cancelled,
    Event.CommandAccepted: _command_accepted,
    Event.CommandRejected: _command_rejected,
    Event.TrackerDelivered: _tracker_delivered,
    Event.RunPaused: _run_paused,
    Event.RunResumed: _run_resumed,
    Event.RunFinished: _run_finished,
    Event.PullRequestDescribed: _describer_finished,
    Event.DescriberFailed: _describer_finished,
}
