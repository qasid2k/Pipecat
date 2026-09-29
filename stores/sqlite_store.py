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
from datetime import date, timedelta
from pathlib import Path
from typing import Sequence

from loguru import logger

from core.records import CallFilter, CallRecord, CallStore, TurnRecord

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
    node_id           TEXT,
    agent_speaking_at_end INTEGER,
    time_to_greeting_s    REAL,
    reply_turns           INTEGER,
    reply_median_s        REAL,
    reply_max_s           REAL
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
    "agent_speaking_at_end", "time_to_greeting_s",
    "reply_turns", "reply_median_s", "reply_max_s",
]

# Columns added AFTER databases already existed in the field, oldest first.
# `CREATE TABLE IF NOT EXISTS` never touches an existing table, so each of these
# is also ALTERed onto an older calls.db at start-up (see _migrate). The rules,
# which keep a live database safe: only ever ADD, never drop or rename; the type
# has no NOT NULL and no default other than NULL, so old rows read as "not
# recorded" rather than inventing a value; and anything added to SCHEMA's calls
# table must also be appended here.
ADDED_CALL_COLUMNS: list[tuple[str, str]] = [
    ("agent_speaking_at_end", "INTEGER"),  # IMP-013
    ("time_to_greeting_s", "REAL"),        # IMP-013
    ("reply_turns", "INTEGER"),            # IMP-016
    ("reply_median_s", "REAL"),            # IMP-016
    ("reply_max_s", "REAL"),               # IMP-016
]


def _next_day(day: str) -> str:
    """'2026-09-28' -> '2026-09-29'. The API has already checked the format."""
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


class SqliteCallStore(CallStore):
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._conn: sqlite3.Connection | None = None
        # Columns this start-up had to add to an older database (for tests and
        # the start-up log). Empty on a fresh or already-current database.
        self.migrated: list[str] = []

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
        self.migrated = self._migrate(self._conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> list[str]:
        """Add any ADDED_CALL_COLUMNS an older calls table is missing.

        Idempotent: it compares against what is actually there, so starting
        twice adds nothing the second time. Additive only, so existing rows are
        never rewritten. SQLite's ALTER TABLE ADD COLUMN is a quick metadata
        change, not a copy of the table.
        """
        present = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
        added = []
        for name, decl in ADDED_CALL_COLUMNS:
            if name not in present:
                conn.execute(f"ALTER TABLE calls ADD COLUMN {name} {decl}")
                logger.info(f"Records: added column calls.{name} ({decl}) to an older database")
                added.append(name)
        conn.commit()
        return added

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

    # -- reading: the supervisor page's call history --------------------------
    # Columns the page shows. Deliberately not SELECT *: file paths and frame
    # counters are for investigation, not for a list a browser renders.
    _RECENT_COLUMNS = (
        "call_id", "started_at", "ended_at", "duration_s", "caller_id",
        "persona", "end_reason", "cause", "transferred_to",
    )

    async def recent_calls(self, limit: int, tenant_id: str) -> list[dict]:
        return await asyncio.to_thread(self._read_recent, limit, tenant_id)

    async def search_calls(self, limit: int, tenant_id: str, filters: CallFilter) -> list[dict]:
        return await asyncio.to_thread(self._read_recent, limit, tenant_id, filters)

    async def call_detail(self, call_id: str, tenant_id: str) -> dict | None:
        return await asyncio.to_thread(self._read_detail, call_id, tenant_id)

    def _reader(self) -> sqlite3.Connection | None:
        if not self._path.exists():
            return None  # nothing recorded yet -- and don't create an empty file
        conn = sqlite3.connect(f"file:{self._path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _where(tenant_id: str, f: CallFilter | None) -> tuple[str, list]:
        """The WHERE clause for a filter, with every value a bound parameter --
        these come straight from a browser, so nothing is ever pasted into SQL."""
        clauses, params = ["tenant_id = ?"], [tenant_id]
        if f is not None:
            if f.since:
                clauses.append("started_at >= ?")
                params.append(f.since)
            if f.until:
                # Inclusive: everything before the START of the next day.
                # started_at is ISO text, so this compares correctly as text.
                clauses.append("started_at < ?")
                params.append(_next_day(f.until))
            if f.persona:
                clauses.append("persona = ?")
                params.append(f.persona)
            if f.transferred is True:
                clauses.append("transferred_to IS NOT NULL")
            elif f.transferred is False:
                clauses.append("transferred_to IS NULL")
            if f.caller:
                # Literal substring: % and _ typed into a search box mean
                # themselves, not "anything".
                escaped = f.caller.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append("caller_id LIKE ? ESCAPE '\\'")
                params.append(f"%{escaped}%")
        return " AND ".join(clauses), params

    def _read_recent(self, limit: int, tenant_id: str, filters: CallFilter | None = None) -> list[dict]:
        """Its OWN short-lived, read-only connection, never the writer's.

        The writer's connection belongs to the RecordWriter task; sharing it
        across threads would need a lock the write path must then wait on. WAL
        mode lets this reader run alongside the writer without either blocking
        the other, and `mode=ro` means a bug here cannot modify a record.
        """
        conn = self._reader()
        if conn is None:
            return []
        where, params = self._where(tenant_id, filters)
        try:
            rows = conn.execute(
                f"SELECT {', '.join(self._RECENT_COLUMNS)} FROM calls "
                f"WHERE {where} ORDER BY started_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def _read_detail(self, call_id: str, tenant_id: str) -> dict | None:
        conn = self._reader()
        if conn is None:
            return None
        try:
            row = conn.execute(
                f"SELECT {', '.join(self._RECENT_COLUMNS)}, uniqueid, linkedid, "
                "conversation_path FROM calls WHERE call_id = ? AND tenant_id = ?",
                (call_id, tenant_id),
            ).fetchone()
            if row is None:
                return None
            turns = conn.execute(
                "SELECT speaker, text, at FROM turns WHERE call_id = ? ORDER BY seq",
                (call_id,),
            ).fetchall()
        finally:
            conn.close()
        return {"call": dict(row), "turns": [dict(t) for t in turns]}

    async def close(self) -> None:
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
