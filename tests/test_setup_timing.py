"""Where the silence before the greeting goes (IMP-002).

Callers hear 3-6 s of nothing before the agent speaks, and nobody knew which
step costs it. Each call now logs a `setup:` line splitting that wait into
steps, and /metrics carries the running total.

What these tests CAN prove: the arithmetic, that the write thread stamps the
first REAL frame (not the silence keep-alive it sends from the start), and that
the total reaches /metrics. What they cannot: the real numbers. Those only
exist on a real call, which is the point of the item.
"""

import asyncio
import time
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import bot
from api.server import ApiServer
from core.config import AppConfig, EngineConfig, TransportConfig
from core.engine import EngineResult
from core.live import Counters, LiveCalls
from core.pool import AgentPool
from engine.timing import format_setup, setup_breakdown
from tests.test_call_loop import FakeSession, FakeTransport, roster
from transports.asterisk import AsteriskCallSession
from transports.audiosocket import AudioSocketConnection


class BreakdownTest(unittest.TestCase):
    def test_each_step_is_the_gap_since_the_previous_mark(self):
        steps, total = setup_breakdown([
            ("connected", 10.0), ("engine_start", 10.5),
            ("pipeline_started", 12.0), ("first_speech", 13.25),
        ])
        self.assertEqual(
            steps,
            [("connected→engine_start", 500), ("engine_start→pipeline_started", 1500),
             ("pipeline_started→first_speech", 1250)],
        )
        self.assertEqual(total, 3.25)

    def test_a_missing_mark_is_skipped_not_zeroed(self):
        # A direct (non-ARI) call has no correlation step; its time must land
        # in the next step rather than vanish.
        steps, _ = setup_breakdown([("connected", 1.0), ("correlated", None), ("engine_start", 2.0)])
        self.assertEqual(steps, [("connected→engine_start", 1000)])

    def test_no_greeting_means_no_total(self):
        # The caller hung up before the agent spoke. That is not a 0 s greeting,
        # and averaging it in as one would flatter the number.
        _, total = setup_breakdown([("connected", 1.0), ("first_speech", None)])
        self.assertIsNone(total)

    def test_the_log_line_names_every_step(self):
        line = format_setup([("a→b", 12), ("b→c", 3400)], 3.412)
        self.assertEqual(line, "setup: a→b 12 ms | b→c 3400 ms | first speech after 3412 ms")
        self.assertIn("no greeting sent", format_setup([("a→b", 12)], None))


class FirstSpeechStampTest(unittest.TestCase):
    """The write thread sends silence from the moment the call connects, so
    `first_speech` must be the first AGENT frame, not the first frame."""

    def write(self, agent_frame_on_send: int | None) -> AudioSocketConnection:
        sock = mock.MagicMock()
        io = AudioSocketConnection(sock, mock.MagicMock())
        sends = []

        def sendall(data):
            sends.append(time.monotonic())
            if agent_frame_on_send is not None and len(sends) == agent_frame_on_send:
                io._outgoing.put_nowait(b"\x01" * 320)
            if len(sends) >= 3:
                io._running = False

        sock.sendall.side_effect = sendall
        io._write_loop()
        io.sends = sends
        return io

    def test_silence_alone_leaves_it_unset(self):
        self.assertIsNone(self.write(agent_frame_on_send=None).first_real_out_at)

    def test_it_is_stamped_on_the_first_agent_frame(self):
        io = self.write(agent_frame_on_send=1)  # queued after send 1, sent as send 2
        self.assertIsNotNone(io.first_real_out_at)
        self.assertGreaterEqual(io.first_real_out_at, io.sends[1])

    def test_connected_is_stamped_at_construction(self):
        before = time.monotonic()
        io = AudioSocketConnection(mock.MagicMock(), mock.MagicMock())
        self.assertGreaterEqual(io.connected_at, before)


class SessionMarksTest(unittest.TestCase):
    def test_asterisk_session_reports_its_marks(self):
        io = AudioSocketConnection(mock.MagicMock(), mock.MagicMock())
        io.first_real_out_at = io.connected_at + 2.0
        session = AsteriskCallSession(
            io=io, addr=("127.0.0.1", 1), controller=None, ari_call=None,
            transfer_context="transfer",
        )
        marks = session.setup_marks()
        self.assertEqual(marks["connected"], io.connected_at)
        self.assertGreaterEqual(marks["correlated"], io.connected_at)
        self.assertEqual(marks["first_speech"], io.connected_at + 2.0)

    def test_the_default_contract_is_empty(self):
        self.assertEqual(FakeSession("x").setup_marks(), {})


class GreetingCountersTest(unittest.IsolatedAsyncioTestCase):
    async def run_call(self, result: EngineResult) -> Counters:
        counters = Counters()

        class Engine:
            async def run(self, session):
                return result

        config = AppConfig(transport=TransportConfig(), engine=EngineConfig())
        with mock.patch.object(bot, "create_engine_for_persona", side_effect=lambda *a, **k: Engine()):
            await bot.run_call(
                config, AgentPool(roster(1)), FakeTransport(), FakeSession("c"),
                counters=counters,
            )
        return counters

    async def test_a_greeted_call_is_counted(self):
        c = await self.run_call(EngineResult(time_to_greeting_s=3.5))
        self.assertEqual((c.greetings_total, c.greeting_seconds_total), (1, 3.5))

    async def test_an_ungreeted_call_is_not(self):
        c = await self.run_call(EngineResult())
        self.assertEqual((c.greetings_total, c.greeting_seconds_total), (0, 0.0))

    async def test_metrics_expose_it_as_a_summary(self):
        counters = Counters(greetings_total=4, greeting_seconds_total=14.0)
        server = ApiServer(pool=AgentPool(roster(1)), live=LiveCalls(), counters=counters)
        app = web.Application()
        app.add_routes([web.get("/metrics", server._metrics)])
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            text = await (await client.get("/metrics")).text()
        finally:
            await client.close()
        self.assertIn("# TYPE voiceagent_time_to_greeting_seconds summary", text)
        self.assertIn("voiceagent_time_to_greeting_seconds_sum 14.000", text)
        self.assertIn("voiceagent_time_to_greeting_seconds_count 4", text)


if __name__ == "__main__":
    unittest.main()
