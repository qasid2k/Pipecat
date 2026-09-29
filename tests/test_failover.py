"""When the AI model fails, the caller hears an apology and a human (IMP-008).

Seen live on 2026-09-28: a caller asked for billing, Gemini hung for 16 s and
then returned "503 high demand", and the caller heard nothing until they gave
up. Pipecat treats that error as non-fatal and simply stops talking.

The watchdog here notices two things -- the model reporting an error, or the
model saying nothing for `failover.timeout_s` after being asked -- and hands
the call over: an apology, then a transfer to a human (or a goodbye on a call
that can't be transferred).

"Answered" means the FIRST REAL OUTPUT: reply text, or the start of a tool
call. Not Pipecat's LLMFullResponseStartFrame: the Gemini service sends that
before it has even called Gemini, so it would reset the timer on every hang.
"""

import asyncio
import unittest
from dataclasses import replace

from pipecat.frames.frames import (
    ErrorFrame,
    FunctionCallsStartedFrame,
    LLMContextFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.tests.utils import run_test

from core.config import ConfigError, EngineConfig, FailoverConfig, _load_failover
from engine.failover import LLMWatchdog, around_llm


class Recorder:
    def __init__(self):
        self.reasons: list[str] = []

    async def __call__(self, reason: str):
        self.reasons.append(reason)


class WatchdogTest(unittest.IsolatedAsyncioTestCase):
    def dog(self, timeout=0.05):
        rec = Recorder()
        return LLMWatchdog(timeout_s=timeout, on_fail=rec), rec

    async def test_silence_after_a_request_fails_over(self):
        dog, rec = self.dog()
        dog.request_sent()
        await asyncio.sleep(0.1)
        self.assertEqual(len(rec.reasons), 1)
        self.assertIn("no reply", rec.reasons[0])

    async def test_an_answer_in_time_cancels_it(self):
        dog, rec = self.dog()
        dog.request_sent()
        dog.responded()
        await asyncio.sleep(0.1)
        self.assertEqual(rec.reasons, [])

    async def test_a_model_error_fails_over_at_once(self):
        dog, rec = self.dog(timeout=10)
        dog.request_sent()
        await dog.failed("503 high demand")
        self.assertEqual(rec.reasons, ["model error: 503 high demand"])

    async def test_it_fires_once_per_call(self):
        dog, rec = self.dog()
        dog.request_sent()
        await dog.failed("first")
        await dog.failed("second")
        dog.request_sent()
        await asyncio.sleep(0.1)
        self.assertEqual(len(rec.reasons), 1)
        self.assertTrue(dog.tripped)

    async def test_each_new_request_restarts_the_clock(self):
        dog, rec = self.dog(timeout=0.08)
        dog.request_sent()
        await asyncio.sleep(0.05)
        dog.responded()
        dog.request_sent()  # the next turn gets a full timeout of its own
        await asyncio.sleep(0.05)
        self.assertEqual(rec.reasons, [])
        await asyncio.sleep(0.06)
        self.assertEqual(len(rec.reasons), 1)

    async def test_stop_cancels_a_pending_timer(self):
        dog, rec = self.dog()
        dog.request_sent()
        dog.stop()
        await asyncio.sleep(0.1)
        self.assertEqual(rec.reasons, [])


class TapsTest(unittest.IsolatedAsyncioTestCase):
    """The two small processors that sit either side of the LLM."""

    def setUp(self):
        self.rec = Recorder()
        self.dog = LLMWatchdog(timeout_s=10, on_fail=self.rec)
        self.llm = FrameProcessor()  # stands in for the LLM service
        self.before, _, self.after = around_llm(self.llm, self.dog)

    async def asyncTearDown(self):
        self.dog.stop()

    async def test_a_request_to_the_model_starts_the_clock(self):
        await run_test(self.before, frames_to_send=[LLMContextFrame(context=LLMContext())],
                       expected_down_frames=[LLMContextFrame])
        self.assertTrue(self.dog.waiting)

    async def test_only_the_models_own_errors_count(self):
        self.dog.request_sent()
        other = FrameProcessor()
        # Called directly: Pipecat's run_test harness delivers upstream error
        # frames before its pipeline has started and drops them, which a real
        # call never does (errors come after start-up).
        await self.before.inspect(ErrorFrame(error="tts hiccup", processor=other), FrameDirection.UPSTREAM)
        await self.before.inspect(ErrorFrame(error="503 high demand", processor=self.llm), FrameDirection.UPSTREAM)
        self.assertEqual(self.rec.reasons, ["model error: 503 high demand"])

    async def test_reply_text_counts_as_an_answer_but_the_start_marker_does_not(self):
        self.dog.request_sent()
        await run_test(self.after, frames_to_send=[LLMFullResponseStartFrame()],
                       expected_down_frames=[LLMFullResponseStartFrame])
        self.assertTrue(self.dog.waiting)
        await run_test(self.after, frames_to_send=[LLMTextFrame("Sure.")],
                       expected_down_frames=[LLMTextFrame])
        self.assertFalse(self.dog.waiting)

    async def test_a_tool_call_counts_as_an_answer(self):
        self.dog.request_sent()
        await run_test(self.after, frames_to_send=[FunctionCallsStartedFrame(function_calls=[])],
                       expected_down_frames=[FunctionCallsStartedFrame])
        self.assertFalse(self.dog.waiting)

    async def test_a_late_reply_after_the_handover_is_not_spoken(self):
        # The apology is already playing; a reply that finally arrives must not
        # talk over it.
        await self.dog.failed("timeout")
        await run_test(self.after, frames_to_send=[LLMTextFrame("Sorry, I was slow!")],
                       expected_down_frames=[])

    def test_the_llm_sits_between_the_two_taps(self):
        before, llm, after = around_llm(self.llm, self.dog)
        self.assertIs(llm, self.llm)
        self.assertIsNot(before, after)


class FailoverConfigTest(unittest.TestCase):
    def test_defaults(self):
        f = _load_failover({})
        self.assertTrue(f.enabled)
        self.assertEqual(f.timeout_s, 6.0)
        self.assertEqual(f.department, "human")
        self.assertTrue(f.apology_text and f.goodbye_text)
        self.assertEqual(f, FailoverConfig())
        self.assertEqual(EngineConfig().failover, FailoverConfig())

    def test_values_are_read(self):
        f = _load_failover({"timeout_s": 8, "department": "support", "enabled": False})
        self.assertEqual((f.timeout_s, f.department, f.enabled), (8.0, "support", False))

    def test_bad_values_are_refused(self):
        for bad in ({"timeout_s": 1}, {"timeout_s": 60}, {"timeout_s": "6"},
                    {"department": ""}, {"apology_text": " "}, {"enabled": "yes"},
                    {"surprise": 1}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                _load_failover(bad)

    def test_an_unknown_department_is_refused_by_the_engine(self):
        from engine.pipecat_engine import PipecatEngine

        cfg = replace(EngineConfig(), failover=replace(FailoverConfig(), department="accounts"))
        with self.assertRaises(ConfigError):
            PipecatEngine(cfg)


if __name__ == "__main__":
    unittest.main()
