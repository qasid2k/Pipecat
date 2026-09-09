"""SQLite call-record store.

WHY SQLITE AND NOT POSTGRES
---------------------------
The plan named PostgreSQL, on the reasoning that Stage F needs concurrent
writers from several nodes and switching later costs a migration. That reasoning
still holds for Stage F. It was written before checking whether there was a
Postgres instance to develop against -- and there is not.

Writing `asyncpg` code that has never been run, against a database that does not
exist yet, and landing it in a service that answers real calls, is precisely the
mistake [[decisions]] 025 was written about: unverifiable code proves nothing
while still costing maintenance. SQLite is in the standard library, needs no
server, no new pinned dependency on two machines, and can be tested properly
today.

What this buys and what it costs:

  * **Buys**: real persistence and real queries now, on the single node this
    service actually is, with the store behind an interface so the schema and
    the writer are settled before the driver question matters.
  * **Costs**: SQLite takes one writer at a time. Fine here -- writes are a
    handful per call, and they already go through a single writer task -- but it
    is not the answer for Stage F's many nodes.

Postgres becomes a second implementation of the same `CallStore` when there is an
instance to verify it against. The interface, the record shapes and the writer
do not change; only this file gets a sibling.

WHY EVERY CALL GOES THROUGH A THREAD
------------------------------------
`sqlite3` is synchronous. Called directly from an `async def` it would block the
event loop -- the hazard behind B-001 and B-011. Every database call here is
wrapped in `asyncio.to_thread`, so the loop stays free even if the disk stalls.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

from core.records import CallRecord, CallStore, TurnRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    call_id           TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    duration_s        REAL NOT NULL DEFAULT 0,
    caller_id         TEXT,
    uniqueid          TEXT,
    linkedid          TEXT,
    persona           TEXT,
    voice             TEXT,
    llm_model         TEXT,
    end_reason        TEXT,
    cause             TEXT,
    transferred_to    TEXT,
    frames_in         INTEGER NOT NULL DEFAULT 0,
    frames_out        INTEGER NOT NULL DEFAULT 0,
    frames_out_real   INTEGER NOT NULL DEFAULT 0,
    frames_dropped    INTEGER NOT NULL DEFAULT 0,
    pacer_slips       INTEGER NOT NULL DEFAULT 0,
    transcript_path   TEXT,
    conversation_path TEXT,
    node_id           TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    call_id  TEXT NOT NULL,
    seq      INTEGER NOT NULL,
    at       TEXT NOT NULL,
    speaker  TEXT NOT NULL,
    text     TEXT NOT NULL,
    language TEXT,
    PRIMARY KEY (call_id, seq)
);

-- The indexes match the questions this table exists to answer: how busy were we
-- on a given day, how much work did each agent take, and how often did we hand
-- over instead of handling it.
CREATE INDEX IF NOT EXISTS idx_calls_started  ON calls (started_at);
CREATE INDEX IF NOT EXISTS idx_calls_tenant   ON calls (tenant_id, started_at);
CREATE INDEX IF NOT EXISTS idx_calls_persona  ON calls (persona);
CREATE INDEX IF NOT EXISTS idx_calls_transfer ON calls (transferred_to);
-- The CDR join key. Without an index this is the query that gets slow first,
-- because it is the one an investigation always starts from.
CREATE INDEX IF NOT EXISTS idx_calls_uniqueid ON calls (uniqueid);
"""

_CALL_COLUMNS = [
    "call_id", "tenant_id", "started_at", "ended_at", "duration_s",
    "caller_id", "uniqueid", "linkedid", "persona", "voice", "llm_model",
    "end_reason", "cause", "transferred_to",
    "frames_in", "frames_out", "frames_out_real", "frames_dropped",
    "pacer_slips",
    "transcript_path", "conversation_path", "node_id",
]


class SqliteCallStore(CallStore):
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._conn: sqlite3.Connection | None = None

    @property
    def describe(self) -> str:
        return f"sqlite -> {self._path}"

    async def start(self) -> None:
        await asyncio.to_thread(self._connect)

    def _connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because every call runs in a to_thread worker,
        # and those are not guaranteed to be the same thread twice. Safe here
        # only because the RecordWriter serialises all access through one task.
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        # WAL lets a reader (a dashboard, a query someone is running) work while
        # we write, instead of blocking on the writer's lock.
        self._conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL rather than FULL: one fsync per checkpoint instead of one per
        # transaction. A crash can lose the last few records; that is the right
        # trade for evidence, and it keeps the writer from being disk-bound.
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    async def save_call(self, record: CallRecord) -> None:
        await asyncio.to_thread(self._save_call, record)

    def _save_call(self, record: CallRecord) -> None:
        if self._conn is None:
            raise RuntimeError("store is not started")
        data = asdict(record)
        # Datetimes as ISO-8601 UTC strings: SQLite has no date type, and a
        # sortable text timestamp is both queryable and readable by eye.
        for key in ("started_at", "ended_at"):
            if data[key] is not None:
                data[key] = data[key].isoformat(timespec="milliseconds")
        # INSERT OR REPLACE, not INSERT: a call is written once at the end today,
        # but making it idempotent means a retry or a future mid-call update
        # cannot produce two rows for one call.
        placeholders = ", ".join("?" for _ in _CALL_COLUMNS)
        self._conn.execute(
            f"INSERT OR REPLACE INTO calls ({', '.join(_CALL_COLUMNS)}) "
            f"VALUES ({placeholders})",
            [data[c] for c in _CALL_COLUMNS],
        )
        self._conn.commit()

    async def save_turns(self, turns: Sequence[TurnRecord]) -> None:
        if not turns:
            return
        await asyncio.to_thread(self._save_turns, turns)

    def _save_turns(self, turns: Sequence[TurnRecord]) -> None:
        if self._conn is None:
            raise RuntimeError("store is not started")
        self._conn.executemany(
            "INSERT OR REPLACE INTO turns "
            "(call_id, seq, at, speaker, text, language) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (t.call_id, t.seq, t.at.isoformat(timespec="milliseconds"),
                 t.speaker, t.text, t.language)
                for t in turns
            ],
        )
        self._conn.commit()

    async def close(self) -> None:
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
