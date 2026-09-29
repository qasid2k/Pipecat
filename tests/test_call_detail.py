"""Call detail and search for the supervisor page (IMP-007, slice 2 of IMP-004).

Two things a supervisor could not do before: read a finished call's whole
conversation without SSH, and narrow the list of calls down.

Where the transcript comes from matters. The `turns` table holds only what the
CALLER said (the recorder sits between STT and the LLM, so it never sees the
agent's words). The full two-sided conversation is in the call's
`conversation.json`, whose path is on the call row. So the detail view reads
that file -- in a thread, and only if it lives inside the recordings directory
-- and falls back to the caller-only turns if it is gone.
"""

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api import server as api_server
from api.server import ApiServer, normalize_conversation
from core.live import Counters, LiveCalls
from core.pool import AgentPool
from core.records import CallFilter, CallRecord, CallStore, NullCallStore, RecordWriter, TurnRecord
from stores.sqlite_store import SqliteCallStore
from tests.test_api import roster

T0 = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)


def record(call_id, day, persona="Sarah", caller="+44100", transferred=None, tenant="techbridge",
           conversation_path=""):
    start = T0 + timedelta(days=day)
    return CallRecord(
        call_id=call_id, tenant_id=tenant, started_at=start,
        ended_at=start + timedelta(seconds=30), duration_s=30.0, caller_id=caller,
        persona=persona, cause="caller hung up", transferred_to=transferred,
        conversation_path=conversation_path,
    )


class StoreFiltersTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = SqliteCallStore(Path(self.dir.name) / "calls.db")
        await self.store.start()
        for r in [
            record("a", 0, persona="Sarah", caller="+44100"),
            record("b", 1, persona="Daniel", caller="+44200", transferred="billing"),
            record("c", 2, persona="Sarah", caller="100_%", transferred="sales"),
            record("x", 2, tenant="acme"),
        ]:
            await self.store.save_call(r)

    async def asyncTearDown(self):
        await self.store.close()
        self.dir.cleanup()

    async def ids(self, **f):
        rows = await self.store.search_calls(limit=50, tenant_id="techbridge", filters=CallFilter(**f))
        return [r["call_id"] for r in rows]

    async def test_by_agent(self):
        self.assertEqual(await self.ids(persona="Sarah"), ["c", "a"])

    async def test_by_outcome(self):
        self.assertEqual(await self.ids(transferred=True), ["c", "b"])
        self.assertEqual(await self.ids(transferred=False), ["a"])

    async def test_by_caller_substring(self):
        self.assertEqual(await self.ids(caller="4420"), ["b"])

    async def test_caller_search_treats_wildcards_literally(self):
        # '%' and '_' are SQL LIKE wildcards; typed in a search box they mean
        # themselves. Unescaped, "_" would match every caller.
        self.assertEqual(await self.ids(caller="_%"), ["c"])

    async def test_by_date_range_inclusive(self):
        self.assertEqual(await self.ids(since="2026-09-28", until="2026-09-28"), ["b"])
        self.assertEqual(await self.ids(since="2026-09-28"), ["c", "b"])

    async def test_filters_combine(self):
        self.assertEqual(await self.ids(persona="Sarah", transferred=True), ["c"])

    async def test_no_filter_is_the_plain_list(self):
        self.assertEqual(await self.ids(), ["c", "b", "a"])


class StoreDetailTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = SqliteCallStore(Path(self.dir.name) / "calls.db")
        await self.store.start()
        await self.store.save_call(record("a", 0, conversation_path="/rec/a-conversation.json"))
        await self.store.save_call(record("x", 0, tenant="acme"))
        at = T0
        await self.store.save_turns([
            TurnRecord(call_id="a", seq=2, at=at, speaker="caller", text="billing please"),
            TurnRecord(call_id="a", seq=1, at=at, speaker="caller", text="hello"),
        ])

    async def asyncTearDown(self):
        await self.store.close()
        self.dir.cleanup()

    async def test_row_and_caller_turns_in_order(self):
        d = await self.store.call_detail("a", tenant_id="techbridge")
        self.assertEqual(d["call"]["call_id"], "a")
        self.assertEqual(d["call"]["conversation_path"], "/rec/a-conversation.json")
        self.assertEqual([t["text"] for t in d["turns"]], ["hello", "billing please"])

    async def test_unknown_or_other_tenant_is_none(self):
        self.assertIsNone(await self.store.call_detail("nope", tenant_id="techbridge"))
        self.assertIsNone(await self.store.call_detail("x", tenant_id="techbridge"))

    async def test_write_only_stores_have_no_detail(self):
        self.assertIsNone(await NullCallStore().call_detail("a", tenant_id="t"))
        self.assertEqual(
            await NullCallStore().search_calls(limit=5, tenant_id="t", filters=CallFilter()), [])


class NormalizeConversationTest(unittest.TestCase):
    def test_roles_become_speakers_and_tool_traffic_is_dropped(self):
        conv = [
            {"role": "assistant", "content": "Hi, this is Sarah."},
            {"role": "user", "content": "Billing please."},
            {"role": "tool", "content": "{'result': 'Connecting'}"},
            {"role": "assistant", "content": [{"type": "text", "text": "Connecting you now."}]},
            {"role": "model", "parts": [{"text": "Google-shaped reply"}]},
            {"role": "assistant", "content": ""},
        ]
        self.assertEqual(normalize_conversation(conv), [
            {"speaker": "agent", "text": "Hi, this is Sarah."},
            {"speaker": "caller", "text": "Billing please."},
            {"speaker": "agent", "text": "Connecting you now."},
            {"speaker": "agent", "text": "Google-shaped reply"},
        ])


class FakeWriter(RecordWriter):
    def __init__(self, detail=None, rows=None):
        super().__init__(NullCallStore())
        self.recent_calls = mock.AsyncMock(return_value=rows or [])
        self.search_calls = mock.AsyncMock(return_value=rows or [])
        self.call_detail = mock.AsyncMock(return_value=detail)


class EndpointTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.recordings = Path(self.dir.name) / "recordings"
        self.recordings.mkdir()
        self.conv = self.recordings / "a-conversation.json"
        self.conv.write_text(json.dumps({"conversation": [
            {"role": "assistant", "content": "Hi, this is Sarah."},
            {"role": "user", "content": "Billing please."},
        ]}), encoding="utf-8")

    async def asyncTearDown(self):
        self.dir.cleanup()

    async def client(self, writer):
        server = ApiServer(pool=AgentPool(roster(1)), live=LiveCalls(), counters=Counters(),
                           records=writer, tenant_id="techbridge", recordings_dir=self.recordings)
        app = web.Application()
        app.add_routes([web.get("/history", server._history),
                        web.get("/history/{call_id}", server._call_detail)])
        c = TestClient(TestServer(app))
        await c.start_server()
        self.addAsyncCleanup(c.close)
        return c

    def detail(self, path):
        return {"call": {"call_id": "a", "persona": "Sarah", "conversation_path": str(path)},
                "turns": [{"speaker": "caller", "text": "from turns", "at": "x"}]}

    async def test_filters_reach_the_store(self):
        w = FakeWriter()
        c = await self.client(w)
        await c.get("/history?persona=Sarah&outcome=transferred&since=2026-09-01"
                    "&until=2026-09-28&caller=12")
        w.search_calls.assert_awaited_once_with(
            limit=50, tenant_id="techbridge",
            filters=CallFilter(since="2026-09-01", until="2026-09-28", persona="Sarah",
                               transferred=True, caller="12"))
        w.recent_calls.assert_not_awaited()

    async def test_bad_filters_are_400(self):
        c = await self.client(FakeWriter())
        for q in ("since=yesterday", "until=2026-13-01", "outcome=maybe"):
            with self.subTest(q=q):
                self.assertEqual((await c.get(f"/history?{q}")).status, 400)

    async def test_full_transcript_from_the_conversation_file(self):
        c = await self.client(FakeWriter(detail=self.detail(self.conv)))
        body = await (await c.get("/history/a")).json()
        self.assertEqual(body["transcript_source"], "conversation")
        self.assertEqual([t["speaker"] for t in body["transcript"]], ["agent", "caller"])
        # The server's file layout is not the browser's business.
        self.assertNotIn("conversation_path", body["call"])

    async def test_falls_back_to_caller_turns_when_the_file_is_gone(self):
        c = await self.client(FakeWriter(detail=self.detail(self.recordings / "gone.json")))
        body = await (await c.get("/history/a")).json()
        self.assertEqual(body["transcript_source"], "turns")
        self.assertEqual(body["transcript"], [{"speaker": "caller", "text": "from turns"}])

    async def test_a_path_outside_recordings_is_never_read(self):
        outside = Path(self.dir.name) / "secret.json"
        outside.write_text(json.dumps({"conversation": [{"role": "user", "content": "SECRET"}]}))
        c = await self.client(FakeWriter(detail=self.detail(outside)))
        body = await (await c.get("/history/a")).json()
        self.assertNotIn("SECRET", json.dumps(body))
        self.assertEqual(body["transcript_source"], "turns")

    async def test_unknown_call_is_404_and_bad_id_is_400(self):
        c = await self.client(FakeWriter(detail=None))
        self.assertEqual((await c.get("/history/nope")).status, 404)
        self.assertEqual((await c.get("/history/bad%20id!")).status, 400)

    async def test_the_file_is_read_off_the_event_loop(self):
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        real = api_server._read_conversation

        def slow(*a):
            time.sleep(0.3)
            return real(*a)

        c = await self.client(FakeWriter(detail=self.detail(self.conv)))
        task = asyncio.create_task(ticker())
        with mock.patch.object(api_server, "_read_conversation", side_effect=slow):
            await c.get("/history/a")
        task.cancel()
        self.assertGreater(ticks, 5, "the loop stalled while reading the transcript")


class PageTest(unittest.TestCase):
    def test_the_page_has_detail_and_filters(self):
        html = (Path(__file__).resolve().parent.parent / "api" / "dashboard.html").read_text(
            encoding="utf-8")
        for needle in ('id="call-detail"', 'id="f-agent"', 'id="f-outcome"', 'id="f-caller"',
                       'id="f-since"', 'id="f-until"', 'fetch("/history/'):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
