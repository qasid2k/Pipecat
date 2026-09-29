"""The call database can grow (IMP-013).

`CREATE TABLE IF NOT EXISTS` never changes a table that already exists, so
until now a new column added to the schema silently never appeared in the
VM's existing calls.db -- and every write of a record carrying it would fail.
The store now adds missing columns on start, additively only.

It also records two new facts per call that the honest numbers need
([[web-app-design]]): whether the agent was mid-sentence when the call ended,
and how long the caller waited before the greeting.
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import bot
from core.config import AppConfig, EngineConfig, PoolPersona, TransportConfig
from core.engine import EngineResult
from core.records import CallRecord
from stores.sqlite_store import ADDED_CALL_COLUMNS, SqliteCallStore
from tests.test_call_loop import FakeSession
from transports.asterisk import AsteriskCallSession
from transports.audiosocket import AudioSocketConnection

# The calls table exactly as it was before IMP-013 -- what the VM has on disk.
OLD_CALLS = """
CREATE TABLE calls (
    call_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, started_at TEXT NOT NULL,
    ended_at TEXT, duration_s REAL NOT NULL DEFAULT 0, caller_id TEXT, uniqueid TEXT,
    linkedid TEXT, persona TEXT, voice TEXT, llm_model TEXT, end_reason TEXT, cause TEXT,
    transferred_to TEXT, frames_in INTEGER NOT NULL DEFAULT 0,
    frames_out INTEGER NOT NULL DEFAULT 0, frames_out_real INTEGER NOT NULL DEFAULT 0,
    frames_dropped INTEGER NOT NULL DEFAULT 0, pacer_slips INTEGER NOT NULL DEFAULT 0,
    transcript_path TEXT, conversation_path TEXT, node_id TEXT
);
INSERT INTO calls (call_id, tenant_id, started_at, duration_s, caller_id, persona, cause)
VALUES ('old-call', 'techbridge', '2026-09-20T10:00:00.000+00:00', 42, '100', 'Alex', 'caller hung up');
"""

T0 = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)


def columns(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
    finally:
        conn.close()


class MigrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "calls.db"

    async def asyncTearDown(self):
        self.dir.cleanup()

    def make_old_database(self):
        conn = sqlite3.connect(self.path)
        conn.executescript(OLD_CALLS)
        conn.commit()
        conn.close()

    async def test_an_old_database_gains_the_new_columns_and_keeps_its_rows(self):
        self.make_old_database()
        store = SqliteCallStore(self.path)
        await store.start()
        await store.close()
        self.assertTrue({name for name, _ in ADDED_CALL_COLUMNS} <= columns(self.path))
        self.assertEqual(store.migrated, [name for name, _ in ADDED_CALL_COLUMNS])

        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT persona, cause, agent_speaking_at_end, time_to_greeting_s "
            "FROM calls WHERE call_id='old-call'").fetchone()
        conn.close()
        # Old calls read as "not recorded" (NULL), never as "no" (0).
        self.assertEqual(row, ("Alex", "caller hung up", None, None))

    async def test_starting_again_changes_nothing(self):
        self.make_old_database()
        for _ in range(2):
            store = SqliteCallStore(self.path)
            await store.start()
            await store.close()
        self.assertEqual(store.migrated, [])

    async def test_a_fresh_database_needs_no_migration(self):
        store = SqliteCallStore(self.path)
        await store.start()
        await store.close()
        self.assertEqual(store.migrated, [])
        self.assertTrue({name for name, _ in ADDED_CALL_COLUMNS} <= columns(self.path))

    async def test_a_migrated_database_accepts_new_records(self):
        self.make_old_database()
        store = SqliteCallStore(self.path)
        await store.start()
        await store.save_call(CallRecord(
            call_id="new-call", tenant_id="techbridge", started_at=T0,
            ended_at=T0 + timedelta(seconds=30), duration_s=30,
            agent_speaking_at_end=True, time_to_greeting_s=1.75,
        ))
        await store.close()
        conn = sqlite3.connect(self.path)
        row = conn.execute("SELECT agent_speaking_at_end, time_to_greeting_s FROM calls "
                           "WHERE call_id='new-call'").fetchone()
        conn.close()
        self.assertEqual(row, (1, 1.75))


class SpeakingAtEndTest(unittest.TestCase):
    """Was the agent mid-sentence when the call ended? The write thread knows
    when it last sent real speech; the connection knows when it ended."""

    def session(self, last_real_ago: float | None, ended: bool = True) -> AsteriskCallSession:
        io = AudioSocketConnection(mock.MagicMock(), mock.MagicMock())
        now = io.connected_at + 30
        io.last_real_out_at = None if last_real_ago is None else now - last_real_ago
        io.ended_at = now if ended else None
        return AsteriskCallSession(io=io, addr=("127.0.0.1", 1), controller=None,
                                   ari_call=None, transfer_context="transfer")

    def test_speech_right_up_to_the_end_counts(self):
        self.assertIs(self.session(0.1).io_counters()["agent_speaking_at_end"], True)

    def test_a_pause_before_the_end_does_not(self):
        self.assertIs(self.session(2.0).io_counters()["agent_speaking_at_end"], False)

    def test_an_agent_that_never_spoke_was_not_speaking(self):
        self.assertIs(self.session(None).io_counters()["agent_speaking_at_end"], False)

    def test_unknown_while_the_call_is_still_up(self):
        self.assertIsNone(self.session(0.1, ended=False).io_counters()["agent_speaking_at_end"])

    def test_the_write_thread_stamps_the_last_real_frame(self):
        from tests.test_setup_timing import FirstSpeechStampTest
        io = FirstSpeechStampTest().write(agent_frame_on_send=1)
        self.assertIsNotNone(io.last_real_out_at)
        self.assertGreaterEqual(io.last_real_out_at, io.first_real_out_at)

    def test_the_end_is_stamped_when_the_call_ends(self):
        io = AudioSocketConnection(mock.MagicMock(), mock.MagicMock())
        self.assertIsNone(io.ended_at)
        io._signal_end("test")
        first = io.ended_at
        io._signal_end("again")
        self.assertIsNotNone(first)
        self.assertEqual(io.ended_at, first)  # the FIRST end counts


class RecordCarriesTheFactsTest(unittest.TestCase):
    def test_the_call_record_gets_both_facts(self):
        class Session(FakeSession):
            def io_counters(self):
                return {"agent_speaking_at_end": True}

        config = AppConfig(transport=TransportConfig(), engine=EngineConfig())
        record = bot._call_record(
            config, Session("c1"), PoolPersona(name="Alex", voice="v"),
            EngineResult(time_to_greeting_s=1.8), T0, T0 + timedelta(seconds=20),
        )
        self.assertIs(record.agent_speaking_at_end, True)
        self.assertEqual(record.time_to_greeting_s, 1.8)

    def test_a_crashed_engine_leaves_them_unknown(self):
        config = AppConfig(transport=TransportConfig(), engine=EngineConfig())
        record = bot._call_record(config, FakeSession("c1"), PoolPersona(name="Alex", voice="v"),
                                  None, T0, T0 + timedelta(seconds=5))
        self.assertIsNone(record.time_to_greeting_s)
        self.assertIsNone(record.agent_speaking_at_end)


if __name__ == "__main__":
    unittest.main()
