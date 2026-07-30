"""Unit tests for AgentPool.

Stdlib `unittest` on purpose, not pytest: this repo pins its dependencies and
the VM and the laptop must match, so a test framework that needs installing on
both is a cost these tests do not justify. IsolatedAsyncioTestCase gives each
test its own fresh event loop, which matters here -- an asyncio.Lock left over
from another loop is exactly the kind of ghost this file exists to rule out.

    python -m unittest discover -s tests -v
"""

import asyncio
import unittest

from core.config import PoolPersona
from core.pool import AgentPool


def roster(n: int) -> list[PoolPersona]:
    """n personas, distinguishable by name and voice."""
    return [
        PoolPersona(name=f"Agent{i}", voice=f"voice-{i}", system_prompt=f"prompt {i}")
        for i in range(n)
    ]


class TestConstruction(unittest.TestCase):
    def test_capacity_is_the_roster_size(self):
        self.assertEqual(AgentPool(roster(3)).capacity, 3)
        self.assertEqual(AgentPool(roster(1)).capacity, 1)

    def test_empty_roster_is_refused(self):
        # A pool that can never answer a call is a configuration mistake worth
        # failing loudly on, not an unusually quiet service.
        with self.assertRaises(ValueError):
            AgentPool([])

    def test_duplicate_names_are_refused(self):
        dupes = [PoolPersona(name="Alex", voice="a"), PoolPersona(name="alex", voice="b")]
        with self.assertRaises(ValueError):
            AgentPool(dupes)

    def test_starts_all_free(self):
        s = AgentPool(roster(3)).stats()
        self.assertEqual((s.capacity, s.free, s.busy), (3, 3, 0))


class TestAcquireRelease(unittest.IsolatedAsyncioTestCase):
    async def test_acquires_n_distinct_personas(self):
        pool = AgentPool(roster(3))
        got = [await pool.acquire() for _ in range(3)]
        self.assertNotIn(None, got)
        self.assertEqual(len({p.name for p in got}), 3, "same persona handed out twice")

    async def test_n_plus_one_returns_none(self):
        pool = AgentPool(roster(3))
        for _ in range(3):
            self.assertIsNotNone(await pool.acquire())
        self.assertIsNone(await pool.acquire())
        # And stays None -- a full pool does not recover on its own.
        self.assertIsNone(await pool.acquire())

    async def test_full_pool_returns_immediately(self):
        """`acquire()` on a full pool must not block: the caller has to be told
        everyone is busy, not left listening to silence until a slot opens."""
        pool = AgentPool(roster(1))
        await pool.acquire()
        result = await asyncio.wait_for(pool.acquire(), timeout=0.5)
        self.assertIsNone(result)

    async def test_release_restores_availability(self):
        pool = AgentPool(roster(2))
        a = await pool.acquire()
        b = await pool.acquire()
        self.assertIsNone(await pool.acquire())

        self.assertTrue(await pool.release(a))
        again = await pool.acquire()
        self.assertIsNotNone(again)
        self.assertEqual(again.name, a.name)

        await pool.release(b)
        await pool.release(again)
        self.assertEqual(pool.stats().free, 2)

    async def test_double_release_does_not_duplicate(self):
        """The leak that would let two callers share an agent.

        A naive release appends to the free list every time, so releasing twice
        puts one persona in the list twice -- and then capacity is silently 3 on
        a roster of 2 and two callers can be handed the same agent.
        """
        pool = AgentPool(roster(2))
        a = await pool.acquire()

        self.assertTrue(await pool.release(a))
        self.assertFalse(await pool.release(a), "second release should be a no-op")
        self.assertFalse(await pool.release(a))

        self.assertEqual(pool.stats().free, 2)
        self.assertEqual(pool.capacity, 2)

        # The real proof: still only 2 acquirable.
        first = await pool.acquire()
        second = await pool.acquire()
        self.assertIsNone(await pool.acquire())
        self.assertNotEqual(first.name, second.name)

    async def test_releasing_a_stranger_is_ignored(self):
        """A persona that was never in this pool must not enlarge it."""
        pool = AgentPool(roster(2))
        stranger = PoolPersona(name="Nobody", voice="x")
        self.assertFalse(await pool.release(stranger))
        self.assertEqual(pool.stats().free, 2)
        self.assertEqual(pool.capacity, 2)

    async def test_release_never_raises_inside_a_finally(self):
        """release() runs in a `finally`, often with an exception already in
        flight. If it raised, it would replace the original error and the real
        cause of the failure would be lost."""
        pool = AgentPool(roster(1))
        persona = await pool.acquire()
        with self.assertRaises(RuntimeError) as caught:
            try:
                raise RuntimeError("the engine failed")
            finally:
                await pool.release(persona)
                await pool.release(persona)  # the double-release path too
        self.assertEqual(str(caught.exception), "the engine failed")
        self.assertEqual(pool.stats().free, 1)


class TestConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_many_concurrent_acquires_never_double_book(self):
        """50 callers at once against 5 agents: exactly 5 get one, all different."""
        pool = AgentPool(roster(5))
        results = await asyncio.gather(*(pool.acquire() for _ in range(50)))

        winners = [p for p in results if p is not None]
        self.assertEqual(len(winners), 5)
        self.assertEqual(len({p.name for p in winners}), 5, "double-booked")
        self.assertEqual(results.count(None), 45)
        self.assertEqual(pool.stats().free, 0)

    async def test_concurrent_acquire_and_release_churn(self):
        """Sustained load: 200 short calls over 4 agents, all interleaved.

        Each 'call' takes a persona, yields control (so the scheduler really
        does interleave them), and gives it back. Afterwards every persona must
        be free and the roster must be exactly as it started -- no leaks, no
        duplicates.
        """
        pool = AgentPool(roster(4))
        concurrent = 0
        peak = 0
        rejected = 0

        async def one_call():
            nonlocal concurrent, peak, rejected
            persona = await pool.acquire()
            if persona is None:
                rejected += 1
                return
            try:
                concurrent += 1
                peak = max(peak, concurrent)
                await asyncio.sleep(0)  # force a scheduling point mid-"call"
                concurrent -= 1
            finally:
                await pool.release(persona)

        await asyncio.gather(*(one_call() for _ in range(200)))

        self.assertLessEqual(peak, 4, "more calls in flight than the pool allows")
        s = pool.stats()
        self.assertEqual((s.free, s.busy), (4, 0), "a persona leaked")
        self.assertEqual(sorted(s.free_names), [f"Agent{i}" for i in range(4)])
        self.assertGreater(rejected, 0, "expected some callers to hit a full pool")

    async def test_a_released_persona_is_reused_by_the_next_caller(self):
        """Sequential calls, one agent at a time: the freed agent comes back.

        This is the pool half of the privacy requirement. The pool guarantees
        the persona is REUSABLE; it carries no conversation state to reuse,
        because PoolPersona is a frozen description. The clean-context half is
        the engine's, built fresh per call.
        """
        pool = AgentPool(roster(3))
        for _ in range(6):
            persona = await pool.acquire()
            self.assertIsNotNone(persona)
            await pool.release(persona)
        self.assertEqual(pool.stats().free, 3)


class TestStats(unittest.IsolatedAsyncioTestCase):
    async def test_counts_track_acquire_and_release(self):
        pool = AgentPool(roster(3))
        a = await pool.acquire()
        b = await pool.acquire()

        s = pool.stats()
        self.assertEqual((s.capacity, s.free, s.busy), (3, 1, 2))
        self.assertEqual(set(s.busy_names), {a.name, b.name})
        self.assertEqual(s.free + s.busy, s.capacity)

        await pool.release(a)
        s = pool.stats()
        self.assertEqual((s.free, s.busy), (2, 1))
        self.assertEqual(s.busy_names, (b.name,))

    async def test_free_plus_busy_always_equals_capacity(self):
        """The invariant that a leak would break. Checked after every move."""
        pool = AgentPool(roster(3))
        held = []
        for _ in range(3):
            held.append(await pool.acquire())
            s = pool.stats()
            self.assertEqual(s.free + s.busy, s.capacity)
        for p in held:
            await pool.release(p)
            s = pool.stats()
            self.assertEqual(s.free + s.busy, s.capacity)


if __name__ == "__main__":
    unittest.main()
