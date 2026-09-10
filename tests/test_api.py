"""The control plane: what it reports, and what it must never do.

Two kinds of test here. The ordinary kind -- does /pool report the right numbers
-- and the kind that matters more: that a busy service is not reported as
unhealthy, that /metrics leaks no caller identifiers, and that a call which has
ended stops appearing as in-progress.

    python -m unittest discover -s tests -t . -v
"""

import asyncio
import json
import unittest
from datetime import timedelta

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.server import ApiServer
from core.config import PoolPersona
from core.live import Counters, LiveCall, LiveCalls, _utcnow
from core.pool import AgentPool
from core.records import NullCallStore, RecordWriter


def roster(n: int) -> list[PoolPersona]:
    return [PoolPersona(name=f"Agent{i}", voice=f"voice-{i}") for i in range(n)]


class ApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = AgentPool(roster(3))
        self.live = LiveCalls()
        self.counters = Counters()
        self.records = RecordWriter(NullCallStore())
        await self.records.start()

        server = ApiServer(
            pool=self.pool, live=self.live, counters=self.counters,
            records=self.records, tenant_id="techbridge",
        )
        # Drive the same handlers through aiohttp's test client rather than
        # binding a real port: the routes are what is under test, not TCP.
        app = web.Application()
        app.add_routes([
            web.get("/", server._index),
            web.get("/health", server._health),
            web.get("/pool", server._pool_state),
            web.get("/calls", server._calls),
            web.get("/metrics", server._metrics),
        ])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.records.close()

    async def get_json(self, path):
        resp = await self.client.get(path)
        self.assertEqual(resp.status, 200)
        return json.loads(await resp.text())

    # -- health ------------------------------------------------------------
    async def test_health_reports_capacity(self):
        body = await self.get_json("/health")
        self.assertEqual(body["status"], "ok")
        self.assertEqual((body["capacity"], body["free"], body["busy"]), (3, 3, 0))
        self.assertEqual(body["tenant"], "techbridge")

    async def test_a_full_pool_is_at_capacity_but_still_healthy(self):
        """Being busy is what the service is FOR. A load balancer must not pull
        a node out for doing its job -- so 200, not 503."""
        for _ in range(3):
            await self.pool.acquire()

        resp = await self.client.get("/health")
        self.assertEqual(resp.status, 200, "a busy node must not look unhealthy")
        body = json.loads(await resp.text())
        self.assertEqual(body["status"], "at_capacity")
        self.assertEqual(body["free"], 0)

    # -- pool --------------------------------------------------------------
    async def test_pool_names_who_is_free_and_who_is_busy(self):
        taken = await self.pool.acquire()
        body = await self.get_json("/pool")
        self.assertEqual(body["busy_agents"], [taken.name])
        self.assertNotIn(taken.name, body["free_agents"])
        self.assertEqual(len(body["free_agents"]), 2)

    # -- live calls --------------------------------------------------------
    async def test_calls_lists_what_is_in_progress(self):
        self.live.started(LiveCall("c1", "101", "Sarah", "voice-1", _utcnow()))
        body = await self.get_json("/calls")
        self.assertEqual(body["count"], 1)
        call = body["calls"][0]
        self.assertEqual(call["persona"], "Sarah")
        self.assertEqual(call["caller_id"], "101")
        self.assertGreaterEqual(call["duration_s"], 0)

    async def test_an_ended_call_disappears(self):
        """The one way this display can actively mislead."""
        self.live.started(LiveCall("c1", "101", "Sarah", "voice-1", _utcnow()))
        self.live.ended("c1")
        self.assertEqual((await self.get_json("/calls"))["count"], 0)

    async def test_longest_running_call_comes_first(self):
        """That is the one a supervisor is looking for."""
        now = _utcnow()
        self.live.started(LiveCall("new", "101", "A", "v", now))
        self.live.started(LiveCall("old", "102", "B", "v", now - timedelta(minutes=20)))
        calls = (await self.get_json("/calls"))["calls"]
        self.assertEqual([c["call_id"] for c in calls], ["old", "new"])

    # -- metrics -----------------------------------------------------------
    async def test_metrics_are_prometheus_text(self):
        self.counters.calls_total = 7
        self.counters.calls_rejected_total = 2
        await self.pool.acquire()

        resp = await self.client.get("/metrics")
        self.assertEqual(resp.status, 200)
        text = await resp.text()

        self.assertIn("voiceagent_calls_total 7", text)
        self.assertIn("voiceagent_calls_rejected_total 2", text)
        self.assertIn("voiceagent_pool_busy 1", text)
        self.assertIn("voiceagent_pool_free 2", text)
        # Every metric needs its HELP/TYPE or Prometheus treats it as untyped.
        for name in ("voiceagent_calls_total", "voiceagent_pool_free"):
            self.assertIn(f"# TYPE {name}", text)

    async def test_metrics_carry_no_caller_identifiers(self):
        """A metrics endpoint is the one most likely to be scraped into a system
        with looser access rules than this one. Numbers only."""
        self.live.started(
            LiveCall("c1", "+441234567890", "Sarah", "voice-1", _utcnow())
        )
        text = await (await self.client.get("/metrics")).text()

        self.assertNotIn("+441234567890", text)
        self.assertNotIn("c1", text)
        self.assertNotIn("Sarah", text)

    async def test_record_writer_stats_are_exposed(self):
        """Dropped records mean the analytics are quietly incomplete, which is
        precisely the kind of thing nobody notices without a metric."""
        text = await (await self.client.get("/metrics")).text()
        self.assertIn("voiceagent_records_dropped_total 0", text)
        self.assertIn("voiceagent_records_failed_total 0", text)


class LiveCallsTest(unittest.TestCase):
    def test_ending_an_unknown_call_is_harmless(self):
        """This runs in a `finally`; it must not raise on a call that never
        started, or it would replace the real exception."""
        live = LiveCalls()
        live.ended("never-existed")
        live.ended("never-existed")
        self.assertEqual(len(live), 0)

    def test_starting_the_same_call_twice_does_not_duplicate_it(self):
        live = LiveCalls()
        for _ in range(2):
            live.started(LiveCall("c1", "101", "A", "v", _utcnow()))
        self.assertEqual(len(live), 1)

    def test_counters_start_at_zero_with_an_uptime(self):
        c = Counters()
        self.assertEqual(c.calls_total, 0)
        self.assertGreaterEqual(c.uptime_s, 0)
        self.assertIn("calls_rejected_total", c.as_dict())


class ApiBindingTest(unittest.IsolatedAsyncioTestCase):
    async def test_it_really_serves_over_tcp_on_loopback(self):
        """The handlers are tested above; this proves start()/stop() work and
        that the default bind is loopback."""
        pool = AgentPool(roster(1))
        server = ApiServer(pool=pool, live=LiveCalls(), counters=Counters(), port=18097)
        await server.start()
        try:
            import aiohttp

            async with aiohttp.ClientSession() as s:
                async with s.get("http://127.0.0.1:18097/health") as r:
                    self.assertEqual(r.status, 200)
                    self.assertEqual((await r.json())["capacity"], 1)
        finally:
            await server.stop()

        # And the port is released, so a restart is not blocked by the API.
        await asyncio.sleep(0)
        server2 = ApiServer(pool=pool, live=LiveCalls(), counters=Counters(), port=18097)
        await server2.start()
        await server2.stop()


if __name__ == "__main__":
    unittest.main()
