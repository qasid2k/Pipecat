"""Stage C: call records, the writer, and the SQLite store.

The rule these tests exist to hold: **a record is evidence, not the product.**
Losing one is bad; dropping a call because a database was slow, full or
restarting is far worse. So every failure path here is checked for failing SOFT
-- the writer must never block a call, never raise into one, and never let a
store problem become a telephony problem.

    python -m unittest discover -s tests -t . -v
"""

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Sequence

from core.records import (
    CallRecord,
    CallStore,
    NullCallStore,
    RecordWriter,
    TurnRecord,
    utcnow,
)
from stores.sqlite_store import SqliteCallStore


def a_call(call_id="uuid-1", **kw) -> CallRecord:
    started = utcnow()
    base = dict(
        call_id=call_id,
        tenant_id="techbridge",
        started_at=started,
        ended_at=started + timedelta(seconds=38.4),
        duration_s=38.4,
        caller_id="103",
        uniqueid="1787584901.399",
        linkedid="1787584901.396",
        persona="Daniel",
        voice="aura-2-orion-en",
        llm_model="gemini-flash-lite-latest",
        end_reason="caller hung up",
        cause="call ended -- caller hung up",
        frames_in=1904,
        frames_out=1921,
        frames_out_real=402,
    )
    base.update(kw)
    return CallRecord(**base)


class SlowStore(CallStore):
    """A store that takes its time, to prove the writer never makes a call wait."""

    def __init__(self, delay=0.05):
        self.delay = delay
        self.calls: list[CallRecord] = []

    async def start(self):
        pass

    async def save_call(self, record):
        await asyncio.sleep(self.delay)
        self.calls.append(record)

    async def save_turns(self, turns):
        await asyncio.sleep(self.delay)

    async def close(self):
        pass


class BrokenStore(CallStore):
    """A store that fails every write, like a database that is down."""

    async def start(self):
        pass

    async def save_call(self, record):
        raise RuntimeError("database is down")

    async def save_turns(self, turns: Sequence[TurnRecord]):
        raise RuntimeError("database is down")

    async def close(self):
        pass


class SqliteStoreTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sub" / "calls.db"
        self.store = SqliteCallStore(self.path)
        await self.store.start()

    async def asyncTearDown(self):
        await self.store.close()
        self._tmp.cleanup()

    def rows(self, sql):
        conn = sqlite3.connect(self.path)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql)]
        finally:
            conn.close()

    async def test_creates_its_own_schema_and_parent_directory(self):
        """A fresh deployment must not need anyone to run DDL by hand."""
        self.assertTrue(self.path.exists())
        tables = {r["name"] for r in self.rows(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("calls", tables)
        self.assertIn("turns", tables)

    async def test_a_call_round_trips_with_its_join_keys(self):
        await self.store.save_call(a_call())
        row = self.rows("SELECT * FROM calls")[0]

        self.assertEqual(row["call_id"], "uuid-1")
        # The three identifiers that make the row worth having.
        self.assertEqual(row["uniqueid"], "1787584901.399")
        self.assertEqual(row["linkedid"], "1787584901.396")
        self.assertEqual(row["tenant_id"], "techbridge")
        self.assertEqual(row["persona"], "Daniel")
        self.assertAlmostEqual(row["duration_s"], 38.4)
        # Timestamps stored as sortable ISO text, since SQLite has no date type.
        self.assertIn("T", row["started_at"])

    async def test_no_transfer_is_null_not_empty_string(self):
        """`transferred_to IS NULL` has to mean 'the agent handled it', so the
        deflection rate is one query rather than a guess about empty strings."""
        await self.store.save_call(a_call())
        self.assertIsNone(self.rows("SELECT * FROM calls")[0]["transferred_to"])

        await self.store.save_call(a_call(call_id="uuid-2", transferred_to="billing"))
        rows = self.rows("SELECT transferred_to FROM calls ORDER BY call_id")
        self.assertEqual([r["transferred_to"] for r in rows], [None, "billing"])

    async def test_writing_the_same_call_twice_does_not_duplicate_it(self):
        """A retry must not produce two rows for one call."""
        await self.store.save_call(a_call())
        await self.store.save_call(a_call(end_reason="corrected"))
        rows = self.rows("SELECT * FROM calls")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["end_reason"], "corrected")

    async def test_turns_keep_their_order(self):
        now = utcnow()
        await self.store.save_turns([
            TurnRecord("uuid-1", 2, now, "agent", "Putting you through."),
            TurnRecord("uuid-1", 1, now, "caller", "I need billing."),
        ])
        rows = self.rows("SELECT * FROM turns ORDER BY seq")
        self.assertEqual([r["seq"] for r in rows], [1, 2])
        self.assertEqual([r["speaker"] for r in rows], ["caller", "agent"])

    async def test_saving_no_turns_is_a_no_op(self):
        await self.store.save_turns([])
        self.assertEqual(self.rows("SELECT * FROM turns"), [])

    async def test_the_overload_counters_are_persisted(self):
        """These are what a capacity ceiling is measured against, so they have to
        survive into the row rather than only appearing in a log line."""
        await self.store.save_call(a_call(frames_dropped=12, pacer_slips=3))
        row = self.rows("SELECT * FROM calls")[0]
        self.assertEqual(row["frames_dropped"], 12)
        self.assertEqual(row["pacer_slips"], 3)


class RecordWriterTest(unittest.IsolatedAsyncioTestCase):
    async def test_submit_returns_immediately_even_when_the_store_is_slow(self):
        """The whole point of the writer: a slow database must not slow a call."""
        store = SlowStore(delay=0.05)
        writer = RecordWriter(store)
        await writer.start()

        loop = asyncio.get_running_loop()
        started = loop.time()
        for i in range(20):
            writer.submit(a_call(call_id=f"uuid-{i}"))
        elapsed = loop.time() - started

        self.assertLess(elapsed, 0.1, "submit() is waiting on the store")
        await writer.close(timeout=10)
        self.assertEqual(len(store.calls), 20, "records were lost on close")

    async def test_a_broken_store_does_not_stop_later_records(self):
        """The writer task must survive a bad row. If it died on the first
        failure, recording would stop silently for the life of the process."""
        writer = RecordWriter(BrokenStore())
        await writer.start()
        for i in range(3):
            writer.submit(a_call(call_id=f"uuid-{i}"))
        await writer.close()

        self.assertEqual(writer.stats.failed, 3)
        self.assertEqual(writer.stats.written, 0)

    async def test_a_full_queue_drops_records_instead_of_blocking(self):
        """Back-pressure onto a live call is not an option. Losing evidence is."""
        writer = RecordWriter(SlowStore(delay=1), max_queue=5)
        await writer.start()

        loop = asyncio.get_running_loop()
        started = loop.time()
        for i in range(50):
            writer.submit(a_call(call_id=f"uuid-{i}"))
        self.assertLess(loop.time() - started, 0.2, "submit() blocked")

        self.assertGreater(writer.stats.dropped, 0)
        writer._task.cancel()

    async def test_submit_never_raises(self):
        writer = RecordWriter(SlowStore(delay=1), max_queue=1)
        await writer.start()
        for i in range(10):
            writer.submit(a_call(call_id=f"uuid-{i}"))  # must not raise
        writer._task.cancel()

    async def test_close_flushes_what_is_queued(self):
        """At shutdown the queue holds the calls that just ended -- the ones
        worth keeping."""
        store = SlowStore(delay=0.01)
        writer = RecordWriter(store)
        await writer.start()
        writer.submit(a_call(call_id="last-call"))
        await writer.close(timeout=5)
        self.assertEqual([c.call_id for c in store.calls], ["last-call"])

    async def test_end_to_end_through_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calls.db"
            writer = RecordWriter(SqliteCallStore(path))
            await writer.start()
            writer.submit(a_call(call_id="uuid-1", transferred_to="sales"))
            writer.submit([TurnRecord("uuid-1", 1, utcnow(), "caller", "hello")])
            await writer.close()

            conn = sqlite3.connect(path)
            try:
                calls = conn.execute(
                    "SELECT call_id, transferred_to FROM calls").fetchall()
                turns = conn.execute("SELECT call_id, text FROM turns").fetchall()
            finally:
                conn.close()

        self.assertEqual(calls, [("uuid-1", "sales")])
        self.assertEqual(turns, [("uuid-1", "hello")])


class NullStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_everything_and_keeps_nothing(self):
        """Records switched off must be a real store, not a None the call path
        has to branch on at every site."""
        writer = RecordWriter(NullCallStore())
        await writer.start()
        writer.submit(a_call())
        writer.submit([TurnRecord("uuid-1", 1, utcnow(), "caller", "hi")])
        await writer.close()
        self.assertEqual(writer.stats.failed, 0)
        self.assertEqual(writer.stats.written, 2)


if __name__ == "__main__":
    unittest.main()
