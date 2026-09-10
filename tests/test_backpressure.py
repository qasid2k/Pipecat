"""Outbound audio pacing: the back-pressure that must survive any change.

`queue_output` waits when the write thread is behind. That waiting is not an
implementation detail -- it is what stops the agent producing audio faster than
the caller can hear it, and it is what keeps Pipecat's sense of time honest
([[decisions]] 002).

Stage E replaced *how* it waits: it used to park a thread-pool worker for up to
20 ms per frame, 50 times a second, per call, in a pool of `min(32, cpu+4)`
shared with everything else. That was the first hard capacity wall in the
service. These tests pin the behaviour that had to be preserved while removing
it -- and the one that had to be removed.

    python -m unittest discover -s tests -t . -v
"""

import asyncio
import queue
import unittest
from unittest import mock

from transports.audiosocket import MAX_OUT_FRAMES, AudioSocketConnection


def connection() -> AudioSocketConnection:
    """A connection with a fake socket -- no threads started."""
    sock = mock.MagicMock()
    return AudioSocketConnection(sock, asyncio.get_event_loop())


FRAME = b"\x00" * 320


class BackPressureTest(unittest.IsolatedAsyncioTestCase):
    async def test_frames_go_through_while_there_is_room(self):
        io = connection()
        for _ in range(MAX_OUT_FRAMES):
            await asyncio.wait_for(io.queue_output(FRAME), timeout=1)
        self.assertEqual(io._outgoing.qsize(), MAX_OUT_FRAMES)

    async def test_it_waits_when_the_queue_is_full(self):
        """The whole point. If this ever stops waiting, the agent runs ahead of
        the caller and the conversation's timing falls apart."""
        io = connection()
        for _ in range(MAX_OUT_FRAMES):
            await io.queue_output(FRAME)

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(io.queue_output(FRAME), timeout=0.3)

        # And nothing was silently dropped to avoid the wait.
        self.assertEqual(io._outgoing.qsize(), MAX_OUT_FRAMES)

    async def test_consuming_a_frame_releases_the_waiter(self):
        io = connection()
        for _ in range(MAX_OUT_FRAMES):
            await io.queue_output(FRAME)

        waiting = asyncio.create_task(io.queue_output(b"\x11" * 320))
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done(), "should still be waiting for room")

        # What the write thread does on every tick that finds a real frame.
        io._outgoing.get_nowait()
        io._signal_space()

        await asyncio.wait_for(waiting, timeout=1)
        self.assertEqual(io._outgoing.qsize(), MAX_OUT_FRAMES)

    async def test_no_wakeup_is_lost_if_room_appears_mid_attempt(self):
        """The ordering bug this code is written to avoid.

        If `_space` were cleared only *after* a failed put, a consume landing
        between the failure and the wait would set an event we then cleared, and
        the frame would sit there until the next 20 ms tick. Clearing first makes
        that sequence impossible -- so simulating it must still complete.
        """
        io = connection()
        for _ in range(MAX_OUT_FRAMES):
            await io.queue_output(FRAME)

        real_put = io._outgoing.put_nowait

        def put_then_free(payload):
            try:
                real_put(payload)
            except queue.Full:
                # Room appears at the worst possible moment: after the failure,
                # before the caller gets to wait.
                io._outgoing.get_nowait()
                io._signal_space()
                raise

        io._outgoing.put_nowait = put_then_free
        await asyncio.wait_for(io.queue_output(b"\x22" * 320), timeout=1)

    async def test_flushing_releases_a_waiter(self):
        """Barge-in empties the queue; a writer blocked on it must wake, or the
        next frame is stuck for a tick."""
        io = connection()
        for _ in range(MAX_OUT_FRAMES):
            await io.queue_output(FRAME)

        waiting = asyncio.create_task(io.queue_output(b"\x33" * 320))
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())

        io.flush_output()
        await asyncio.wait_for(waiting, timeout=1)

    async def test_waiting_uses_no_thread_pool_worker(self):
        """The capacity wall this change exists to remove.

        A blocking `put` in the default executor occupies a worker for as long
        as it waits. With `min(32, cpu+4)` workers shared across the process,
        a few tens of concurrent calls exhausted the pool and starved everything
        else using `run_in_executor`.
        """
        io = connection()
        loop = asyncio.get_running_loop()
        with mock.patch.object(
            loop, "run_in_executor", side_effect=AssertionError("used a thread pool")
        ):
            for _ in range(MAX_OUT_FRAMES):
                await io.queue_output(FRAME)

            waiting = asyncio.create_task(io.queue_output(FRAME))
            await asyncio.sleep(0.05)
            io._outgoing.get_nowait()
            io._signal_space()
            await asyncio.wait_for(waiting, timeout=1)

    async def test_many_calls_can_wait_at_once(self):
        """50 connections all blocked on a full queue at the same time. Under
        the old design each of these held a pool worker, and the pool has 32."""
        connections = [connection() for _ in range(50)]
        for io in connections:
            for _ in range(MAX_OUT_FRAMES):
                await io.queue_output(FRAME)

        waiters = [asyncio.create_task(io.queue_output(FRAME)) for io in connections]
        await asyncio.sleep(0.1)
        self.assertTrue(all(not w.done() for w in waiters), "none should be through")

        for io in connections:
            io._outgoing.get_nowait()
            io._signal_space()

        await asyncio.wait_for(asyncio.gather(*waiters), timeout=5)


class EndOfCallReleasesWaitersTest(unittest.IsolatedAsyncioTestCase):
    """B-014: a writer blocked on a full queue when the call ends.

    Only the write thread makes room. Once it stops -- caller hung up, socket
    died -- nothing will ever drain the queue again, so a writer waiting there
    waits forever, `engine.run()` never returns, and that call's PERSONA IS
    NEVER RELEASED. Capacity drops by one, silently and permanently.

    Found by `tools/loadtest.py`: 60 agents held with no calls in progress. No
    amount of manual dialling would have shown it -- it needs a full outgoing
    queue at the exact moment of hangup, which happens under concurrency.
    """

    async def full_connection(self) -> AudioSocketConnection:
        io = connection()
        for _ in range(MAX_OUT_FRAMES):
            await io.queue_output(FRAME)
        return io

    async def test_hangup_releases_a_blocked_writer(self):
        io = await self.full_connection()
        waiting = asyncio.create_task(io.queue_output(FRAME))
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done(), "precondition: should be blocked")

        # What the read thread does when Asterisk sends HANGUP.
        io._signal_end("caller hung up")

        await asyncio.wait_for(waiting, timeout=1)

    async def test_stop_releases_a_blocked_writer(self):
        """The shutdown-drain path: session.hangup() calls io.stop()."""
        io = await self.full_connection()
        waiting = asyncio.create_task(io.queue_output(FRAME))
        await asyncio.sleep(0.05)
        self.assertFalse(waiting.done())

        io.stop()

        await asyncio.wait_for(waiting, timeout=1)

    async def test_writing_after_the_call_ended_returns_at_once(self):
        """Not just the already-waiting writer: anything arriving afterwards
        must give up too, or the next frame strands the call instead."""
        io = await self.full_connection()
        io._signal_end("caller hung up")

        await asyncio.wait_for(io.queue_output(FRAME), timeout=1)
        await asyncio.wait_for(io.queue_output(FRAME), timeout=1)

    async def test_the_engine_loop_can_finish_after_a_hangup(self):
        """The property that actually matters, at the level it bit us: an engine
        writing every frame it reads must return when the call ends, so
        `run_call`'s finally can release the persona."""
        io = await self.full_connection()

        done = False

        async def engine_like():
            nonlocal done
            # Blocked here, exactly like SilentEngine and the real pipeline.
            await io.queue_output(FRAME)
            done = True

        task = asyncio.create_task(engine_like())
        await asyncio.sleep(0.05)
        self.assertFalse(done)

        io._signal_end("Asterisk sent HANGUP (caller hung up)")
        await asyncio.wait_for(task, timeout=1)
        self.assertTrue(done, "the call would have hung and leaked its persona")


if __name__ == "__main__":
    unittest.main()
