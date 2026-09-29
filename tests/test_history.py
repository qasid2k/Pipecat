"""Recent calls for the supervisor page (IMP-005, slice 1 of the IMP-004 web app).

Until now the call store could only WRITE: every finished call was saved to
`records/calls.db` and nothing could read it back. A supervisor saw calls in
progress and nothing else. This is the first read path.

The rule that shapes it ([[decisions]] 043): the API runs inside the process
that serves calls, so a slow handler is a dropped call. The read therefore runs
in a thread on its own connection, and one test proves the event loop keeps
running while a deliberately slow read is in flight.
"""

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.server import ApiServer
from core.live import Counters, LiveCalls
from core.pool import AgentPool
from core.records import CallRecord, CallStore, NullCallStore, RecordWriter
from stores.sqlite_store import SqliteCallStore
from tests.test_api import roster

T0 = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)


def record(call_id, minutes, tenant="techbridge", **kw):
    start = T0 + timedelta(minutes=minutes)
    return CallRecord(
        call_id=call_id, tenant_id=tenant, started_at=start,
        ended_at=start + timedelta(seconds=42), duration_s=42.0,
        caller_id="+441234", persona="Sarah", cause="caller hung up", **kw,
    )


class SqliteHistoryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = SqliteCallStore(Path(self.dir.name) / "calls.db")
        await self.store.start()
        for i, cid in enumerate(["first", "second", "third"]):
            await self.store.save_call(record(cid, i, transferred_to="billing" if i == 1 else None))
        await self.store.save_call(record("other-tenant", 10, tenant="acme"))

    async def asyncTearDown(self):
        await self.store.close()
        self.dir.cleanup()

    async def test_newest_first_within_the_limit(self):
        rows = await self.store.recent_calls(limit=2, tenant_id="techbridge")
        self.assertEqual([r["call_id"] for r in rows], ["third", "second"])

    async def test_other_tenants_are_not_shown(self):
        rows = await self.store.recent_calls(limit=50, tenant_id="techbridge")
        self.assertNotIn("other-tenant", [r["call_id"] for r in rows])

    async def test_rows_carry_what_the_page_shows(self):
        row = (await self.store.recent_calls(limit=3, tenant_id="techbridge"))[1]
        self.assertEqual(row["transferred_to"], "billing")
        for key in ("call_id", "started_at", "duration_s", "caller_id", "persona",
                    "end_reason", "cause", "transferred_to"):
            self.assertIn(key, row)
        # Internal paths stay out of an API response.
        self.assertNotIn("transcript_path", row)

    async def test_the_read_does_not_block_the_event_loop(self):
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        real = self.store._read_recent

        def slow_read(*args):
            time.sleep(0.3)  # a slow disk, a big table
            return real(*args)

        task = asyncio.create_task(ticker())
        with mock.patch.object(self.store, "_read_recent", side_effect=slow_read):
            await self.store.recent_calls(limit=5, tenant_id="techbridge")
        task.cancel()
        self.assertGreater(ticks, 5, "the loop stalled during the read")


class NoDatabaseYetTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_missing_database_reads_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            store = SqliteCallStore(Path(d) / "never-created.db")
            self.assertEqual(await store.recent_calls(limit=5, tenant_id="t"), [])
            self.assertFalse((Path(d) / "never-created.db").exists())

    async def test_null_and_base_stores_have_no_history(self):
        self.assertEqual(await NullCallStore().recent_calls(limit=5, tenant_id="t"), [])

        class WriteOnly(CallStore):  # a store written before reads existed
            async def start(self): pass
            async def save_call(self, record): pass
            async def save_turns(self, turns): pass
            async def close(self): pass

        self.assertEqual(await WriteOnly().recent_calls(limit=5, tenant_id="t"), [])

    async def test_the_writer_passes_reads_through(self):
        store = NullCallStore()
        with mock.patch.object(store, "recent_calls", return_value=[{"call_id": "x"}]) as m:
            rows = await RecordWriter(store).recent_calls(limit=7, tenant_id="t")
        self.assertEqual(rows, [{"call_id": "x"}])
        m.assert_called_once_with(limit=7, tenant_id="t")


class HistoryEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def client(self, records):
        server = ApiServer(pool=AgentPool(roster(1)), live=LiveCalls(), counters=Counters(),
                           records=records, tenant_id="techbridge")
        app = web.Application()
        app.add_routes([web.get("/history", server._history), web.get("/api", server._index)])
        c = TestClient(TestServer(app))
        await c.start_server()
        self.addAsyncCleanup(c.close)
        return c

    def writer(self, rows=None, boom=False):
        w = RecordWriter(NullCallStore())
        if boom:
            w.recent_calls = mock.AsyncMock(side_effect=OSError("disk gone"))
        else:
            w.recent_calls = mock.AsyncMock(return_value=rows or [])
        return w

    async def test_returns_the_rows(self):
        w = self.writer([{"call_id": "a"}, {"call_id": "b"}])
        body = await (await (await self.client(w)).get("/history")).json()
        self.assertEqual(body["count"], 2)
        w.recent_calls.assert_awaited_once_with(limit=50, tenant_id="techbridge")

    async def test_the_limit_is_capped(self):
        w = self.writer()
        await (await self.client(w)).get("/history?limit=100000")
        w.recent_calls.assert_awaited_once_with(limit=200, tenant_id="techbridge")

    async def test_a_bad_limit_is_a_400(self):
        resp = await (await self.client(self.writer())).get("/history?limit=lots")
        self.assertEqual(resp.status, 400)

    async def test_records_disabled_is_an_empty_list(self):
        body = await (await (await self.client(None)).get("/history")).json()
        self.assertEqual(body, {"count": 0, "calls": []})

    async def test_a_store_error_is_a_503_not_a_crash(self):
        resp = await (await self.client(self.writer(boom=True))).get("/history")
        self.assertEqual(resp.status, 503)

    async def test_it_is_listed_in_the_index(self):
        body = await (await (await self.client(None)).get("/api")).json()
        self.assertIn("/history", body["endpoints"])


def web_source_and_build() -> tuple[str, str]:
    """The page's React source and the built bundle the VM serves. Since
    IMP-011 the page is React ([[decisions]] 056); the old api/dashboard.html
    is gone. Behaviour is tested in web/ (`npm test`); these check the pieces
    exist in both the source and what actually ships."""
    repo = Path(__file__).resolve().parent.parent
    src = "\n".join(p.read_text(encoding="utf-8") for p in sorted((repo / "web" / "src").rglob("*.ts*"))
                    if ".test." not in p.name)
    built = "\n".join(p.read_text(encoding="utf-8") for p in (repo / "api" / "static" / "assets").glob("*.js"))
    return src, built


class DashboardPageTest(unittest.TestCase):
    def test_the_page_has_the_recent_calls_table(self):
        src, built = web_source_and_build()
        for text in (src, built):
            self.assertIn("Recent calls", text)
            self.assertIn("/history?", text)


if __name__ == "__main__":
    unittest.main()
