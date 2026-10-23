"""The Journal is one SQLite event log per machine, with projections kept in the same transaction.

`append` is the only write path.
It stores the event and applies it to the projection tables before the transaction commits,
so the projections can always be rebuilt from the events.
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factory.clock import Clock
from factory.executor import SANDBOX_FAILURES
from factory.pipeline import TERMINAL_NODES
from factory.states import RunState, TicketState

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
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS pauses (
    run_id TEXT PRIMARY KEY,
    resets_at TEXT NOT NULL
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
    PRIMARY KEY (run_id, id)
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


class Journal:
    def __init__(self, path: Path, clock: Clock) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None)
        self._db.executescript(SCHEMA)
        columns = [row[1] for row in self._db.execute("PRAGMA table_info(tickets)")]
        if "sandbox_losses" not in columns:
            self._db.execute("ALTER TABLE tickets ADD COLUMN sandbox_losses INTEGER NOT NULL DEFAULT 0")
        if "kept_ref" not in columns:
            self._db.execute("ALTER TABLE tickets ADD COLUMN kept_ref TEXT")
        self._clock = clock

    def close(self) -> None:
        self._db.close()

    def append(self, run_id: str, type: str, payload: dict[str, Any]) -> int:
        at = self._clock.now().isoformat()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cursor = self._db.execute(
                "INSERT INTO events (run_id, at, type, payload) VALUES (?, ?, ?, ?)",
                (run_id, at, type, json.dumps(payload)),
            )
            PROJECTORS.get(type, _ignore)(self._db, run_id, at, payload)
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def run(self, run_id: str) -> RunRow | None:
        row = self._db.execute(
            "SELECT id, state, spec_ref, integration_branch, resets_at"
            " FROM runs LEFT JOIN pauses ON pauses.run_id = runs.id WHERE id = ?",
            (run_id,),
        ).fetchone()
        return RunRow(*row) if row else None

    def tickets(self, run_id: str) -> list[TicketRow]:
        rows = self._db.execute(
            "SELECT id, title, state, node, last_stage, round, attempt, sandbox_losses, kept_ref"
            " FROM tickets WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        return [TicketRow(*row) for row in rows]


def _ignore(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    pass


def _run_started(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute(
        "INSERT INTO runs (id, state, spec_ref, integration_branch, started_at) VALUES (?, ?, ?, ?, ?)",
        (run_id, RunState.RUNNING, payload["spec_ref"], payload["integration_branch"], at),
    )


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


def _sandbox_lost(db: sqlite3.Connection, run_id: str, ticket: str) -> None:
    db.execute("UPDATE tickets SET sandbox_losses = sandbox_losses + 1 WHERE run_id = ? AND id = ?", (run_id, ticket))


def _probe_lost(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    _sandbox_lost(db, run_id, payload["ticket"])


def _abandoned_branch_saved(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute(
        "UPDATE tickets SET kept_ref = ? WHERE run_id = ? AND id = ?", (payload["ref"], run_id, payload["ticket"])
    )


def _attempt_finished(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    if payload.get("cause") in SANDBOX_FAILURES:
        _sandbox_lost(db, run_id, payload["ticket"])
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


def _operation_finished(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    if payload["kind"] == "merge" and payload["outcome"] == "merged":
        db.execute(
            "UPDATE tickets SET state = ? WHERE run_id = ? AND id = ?", (TicketState.MERGED, run_id, payload["ticket"])
        )


def _run_paused(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("INSERT OR REPLACE INTO pauses (run_id, resets_at) VALUES (?, ?)", (run_id, payload["resets_at"]))


def _run_resumed(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("DELETE FROM pauses WHERE run_id = ?", (run_id,))


def _run_finished(db: sqlite3.Connection, run_id: str, at: str, payload: dict[str, Any]) -> None:
    db.execute("UPDATE runs SET state = ?, finished_at = ? WHERE id = ?", (payload["state"], at, run_id))
    db.execute("DELETE FROM pauses WHERE run_id = ?", (run_id,))


PROJECTORS: dict[str, Projector] = {
    "RunStarted": _run_started,
    "SpecSnapshotted": _spec_snapshotted,
    "TicketClaimed": _ticket_claimed,
    "NodeEntered": _node_entered,
    "AttemptStarted": _attempt_started,
    "AttemptFinished": _attempt_finished,
    "ProbeLost": _probe_lost,
    "AbandonedBranchSaved": _abandoned_branch_saved,
    "OperationFinished": _operation_finished,
    "RunPaused": _run_paused,
    "RunResumed": _run_resumed,
    "RunFinished": _run_finished,
}
