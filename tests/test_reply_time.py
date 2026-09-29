"""How long the agent takes to reply (IMP-016, the measuring part).

Earlier laptop runs saw 2.3-2.6 s from the caller finishing to the agent
starting to answer, and once 11.6 s; good voice agents are under 1.5 s. On the
VM it had never been measured per call. Now every turn is timed with
Pipecat's own UserBotLatencyObserver (caller actually stopped -> first agent
audio, corrected for the voice detector's delay), and each call keeps a
summary: turns measured, typical (median) and slowest reply.
"""

import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pipecat.frames.frames import BotStartedSpeakingFrame, VADUserStoppedSpeakingFrame
from pipecat.observers.base_observer import FramePushed
from pipecat.observers.user_bot_latency_observer import LatencyBreakdown, TTFBBreakdownMetrics
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

import bot
from api.server import ApiServer
from core.config import AppConfig, EngineConfig, PoolPersona, TransportConfig
from core.engine import EngineResult
from core.live import Counters, LiveCalls
from core.pool import AgentPool
from engine.turn_timing import ReplyStats, format_turn, turn_parts
from stores.sqlite_store import ADDED_CALL_COLUMNS
from tests.test_api import roster
from tests.test_call_loop import FakeSession, FakeTransport

T0 = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)


def ttfb(name, secs):
    return TTFBBreakdownMetrics(processor=name, start_time=0.0, duration_secs=secs)


class ReplyStatsTest(unittest.TestCase):
    def test_summary(self):
        s = ReplyStats()
        for v in (1.2, 0.9, 3.4, 1.5):
            s.add(v)
        self.assertEqual(s.count, 4)
        self.assertAlmostEqual(s.median, 1.35)
        self.assertEqual(s.slowest, 3.4)
        self.assertAlmostEqual(s.total, 7.0)

    def test_no_turns_means_unknown_not_zero(self):
        s = ReplyStats()
        self.assertEqual((s.count, s.median, s.slowest), (0, None, None))


class TurnLineTest(unittest.TestCase):
    def test_breakdown_parts_by_service(self):
        b = LatencyBreakdown(
            ttfb=[ttfb("DeepgramSTTService#0", 0.05), ttfb("GoogleLLMService#0", 0.72),
                  ttfb("DeepgramTTSService#0", 0.21)],
            user_turn_secs=0.94,
        )
        self.assertEqual(turn_parts(b), {"end of turn": 940, "AI first words": 720, "voice": 210})

    def test_the_log_line(self):
        line = format_turn(1.95, {"end of turn": 940, "AI first words": 720, "voice": 210})
        self.assertEqual(line, "turn: reply after 1950 ms (end of turn 940 ms · AI first words 720 ms · voice 210 ms)")
        self.assertEqual(format_turn(1.2, {}), "turn: reply after 1200 ms")


class ObserverWiringTest(unittest.IsolatedAsyncioTestCase):
    """Drive the REAL Pipecat observer with frames and check our handlers
    receive its measurement."""

    async def test_a_turn_is_measured_and_logged(self):
        from engine.pipecat_engine import attach_reply_timing

        stats, lines = ReplyStats(), []
        observer = attach_reply_timing(stats, log=lines.append)
        src, dst = FrameProcessor(), FrameProcessor()

        async def push(frame):
            await observer.on_push_frame(FramePushed(
                source=src, destination=dst, frame=frame,
                direction=FrameDirection.DOWNSTREAM, timestamp=0))

        stopped = VADUserStoppedSpeakingFrame(stop_secs=0.2)
        stopped.timestamp = time.time()
        await push(stopped)
        await asyncio.sleep(0.05)
        await push(BotStartedSpeakingFrame())
        await asyncio.sleep(0.05)  # Pipecat runs event handlers as tasks

        self.assertEqual(stats.count, 1)
        self.assertGreater(stats.median, 0.2)  # includes the VAD's 0.2 s
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("turn: reply after"))


class RecordAndMetricsTest(unittest.IsolatedAsyncioTestCase):
    def test_new_columns_are_migrated(self):
        names = [n for n, _ in ADDED_CALL_COLUMNS]
        for col in ("reply_turns", "reply_median_s", "reply_max_s"):
            self.assertIn(col, names)

    def test_the_record_carries_the_summary(self):
        config = AppConfig(transport=TransportConfig(), engine=EngineConfig())
        r = bot._call_record(config, FakeSession("c1"), PoolPersona(name="Alex", voice="v"),
                             EngineResult(reply_turns=3, reply_median_s=1.4, reply_max_s=2.9),
                             T0, T0 + timedelta(seconds=40))
        self.assertEqual((r.reply_turns, r.reply_median_s, r.reply_max_s), (3, 1.4, 2.9))

    async def test_counters_and_metrics(self):
        counters = Counters()

        class Engine:
            async def run(self, session):
                return EngineResult(reply_turns=2, reply_seconds_total=3.0)

        config = AppConfig(transport=TransportConfig(), engine=EngineConfig())
        from unittest import mock
        with mock.patch.object(bot, "create_engine_for_persona", side_effect=lambda *a, **k: Engine()):
            await bot.run_call(config, AgentPool(roster(1)), FakeTransport(), FakeSession("c"),
                               counters=counters)
        self.assertEqual((counters.replies_total, counters.reply_seconds_total), (2, 3.0))

        server = ApiServer(pool=AgentPool(roster(1)), live=LiveCalls(), counters=counters)
        app = web.Application()
        app.add_routes([web.get("/metrics", server._metrics)])
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            text = await (await client.get("/metrics")).text()
        finally:
            await client.close()
        self.assertIn("# TYPE voiceagent_reply_seconds summary", text)
        self.assertIn("voiceagent_reply_seconds_sum 3.000", text)
        self.assertIn("voiceagent_reply_seconds_count 2", text)


if __name__ == "__main__":
    unittest.main()
