"""Tests for the shutdown drain.

This is code that only ever runs when the service is being taken down, which is
exactly when nobody is watching and exactly when a mistake is expensive: a
half-drained shutdown leaves orphaned Asterisk channels and personas that never
came back. So it is tested rather than trusted.

    python -m unittest discover -s tests -t . -v
"""

import asyncio
import unittest
from unittest import mock

import bot
from core.config import PoolPersona
from core.pool import AgentPool


def roster(n: int) -> list[PoolPersona]:
    return [
        PoolPersona(name=f"Agent{i}", voice=f"voice-{i}", system_prompt=f"prompt {i}")
        for i in range(n)
    ]


class DrainTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot._active_calls.clear()

    def tearDown(self):
        bot._active_calls.clear()

    def track(self, coro) -> asyncio.Task:
        """Register a fake call the way `main`'s accept loop does."""
        task = asyncio.create_task(coro)
        bot._active_calls.add(task)
        task.add_done_callback(bot._active_calls.discard)
        return task

    async def test_no_calls_returns_immediately(self):
        await asyncio.wait_for(bot._drain(30, asyncio.Event()), timeout=0.5)

    async def test_waits_for_a_call_to_finish_on_its_own(self):
        """The point of a drain: a caller mid-sentence is not cut off."""
        finished = False

        async def call():
            nonlocal finished
            await asyncio.sleep(0.1)
            finished = True

        self.track(call())
        await bot._drain(30, asyncio.Event())
        self.assertTrue(finished, "drain returned before the call had finished")
        self.assertEqual(len(bot._active_calls), 0)

    async def test_does_not_wait_the_full_timeout_when_calls_end_early(self):
        """A 30 s timeout must not mean a 30 s shutdown."""
        self.track(asyncio.sleep(0.05))
        loop = asyncio.get_running_loop()
        started = loop.time()
        await bot._drain(30, asyncio.Event())
        self.assertLess(loop.time() - started, 5, "drain waited for the timeout")

    async def test_cancels_calls_that_outlast_the_timeout(self):
        """One caller who never hangs up must not hold a deploy forever."""
        cleaned_up = False

        async def endless_call():
            nonlocal cleaned_up
            try:
                await asyncio.sleep(3600)
            finally:
                cleaned_up = True  # the `finally` that releases the persona

        task = self.track(endless_call())
        await bot._drain(0.05, asyncio.Event())

        self.assertTrue(task.cancelled() or task.done())
        self.assertTrue(cleaned_up, "the call's finally never ran -- persona leaked")

    async def test_a_cancelled_call_still_releases_its_persona(self):
        """The property that matters: cancelling is safe, not destructive."""
        pool = AgentPool(roster(2))
        persona = await pool.acquire()
        self.assertEqual(pool.stats().busy, 1)

        async def endless_call():
            try:
                await asyncio.sleep(3600)
            finally:
                await pool.release(persona)

        self.track(endless_call())
        await bot._drain(0.05, asyncio.Event())
        self.assertEqual(pool.stats().free, 2, "persona leaked on a forced shutdown")

    async def test_force_skips_the_wait(self):
        """A second Ctrl+C means now, not 'after the 30 s you already asked to skip'."""
        force = asyncio.Event()
        force.set()

        self.track(asyncio.sleep(3600))
        loop = asyncio.get_running_loop()
        started = loop.time()
        await bot._drain(3600, force)
        self.assertLess(loop.time() - started, 5, "force did not skip the wait")
        self.assertEqual(len([t for t in bot._active_calls if not t.done()]), 0)

    async def test_drain_survives_a_call_that_raises(self):
        """A failing call must not stop the other calls being drained."""

        async def boom():
            raise RuntimeError("engine died during shutdown")

        ok = False

        async def fine():
            nonlocal ok
            await asyncio.sleep(0.05)
            ok = True

        self.track(boom())
        self.track(fine())
        await bot._drain(30, asyncio.Event())
        self.assertTrue(ok)


class SignalHandlerTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_signal_drains_and_second_forces(self):
        stop, force = asyncio.Event(), asyncio.Event()
        with mock.patch.object(bot.asyncio, "get_running_loop") as get_loop:
            loop = get_loop.return_value
            bot._install_signal_handlers(stop, force)
            # add_signal_handler(sig, request_stop, signame) -- pull the callback
            # back out and drive it, rather than raising real signals in a test.
            self.assertTrue(loop.add_signal_handler.called)
            _sig, request_stop, signame = loop.add_signal_handler.call_args[0]

        request_stop(signame)
        self.assertTrue(stop.is_set())
        self.assertFalse(force.is_set(), "one signal should drain, not cut off")

        request_stop(signame)
        self.assertTrue(force.is_set(), "a second signal should force")

    async def test_falls_back_when_the_loop_has_no_signal_support(self):
        """Windows: the proactor loop raises NotImplementedError here, and that
        is the platform this project is developed on."""
        stop, force = asyncio.Event(), asyncio.Event()
        with mock.patch.object(bot.asyncio, "get_running_loop") as get_loop:
            get_loop.return_value.add_signal_handler.side_effect = NotImplementedError
            with mock.patch.object(bot.signal, "signal") as raw_signal:
                bot._install_signal_handlers(stop, force)
        self.assertTrue(raw_signal.called, "no fallback handler was installed")


if __name__ == "__main__":
    unittest.main()
