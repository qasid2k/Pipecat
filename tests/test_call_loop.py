"""Tests for `bot.run_call` -- the one path a call can take.

The real engine would open Deepgram and Gemini connections, so it is replaced
here with a fake that records which persona it was built for and how long it
ran. What is under test is the WIRING, and the wiring is where the dangerous
bugs live: a persona that never comes back, two callers handed the same agent,
a caller answered when the pool is full.

    python -m unittest discover -s tests -t . -v
"""

import asyncio
import unittest
from unittest import mock

import bot
from core.config import PoolPersona
from core.pool import AgentPool
from core.transport import BaseTransport, CallSession


class FakeSession(CallSession):
    """A call that does nothing, so the loop around it can be tested."""

    def __init__(self, call_id: str):
        self.call_id = call_id
        self.caller_id = "+10000000000"
        self.ended = asyncio.Event()
        self.end_reason = "test"
        self.hangups = 0

    async def read_audio(self):
        return None

    async def write_audio(self, pcm: bytes) -> None:
        pass

    async def transfer(self, destination: str) -> bool:
        return True

    async def hangup(self) -> None:
        self.hangups += 1


class FakeTransport(BaseTransport):
    def __init__(self):
        self.rejected: list[str] = []

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def listen(self):
        raise NotImplementedError

    async def reject(self, call: CallSession) -> None:
        self.rejected.append(call.call_id)
        await call.hangup()


class FakeEngine:
    """Records the persona it was built for. One instance per call, always."""

    instances: list["FakeEngine"] = []

    def __init__(self, persona: PoolPersona, hold: float = 0.0, boom: bool = False):
        self.persona = persona
        self.hold = hold
        self.boom = boom
        self.ran_with: CallSession | None = None
        FakeEngine.instances.append(self)

    async def run(self, session: CallSession) -> None:
        self.ran_with = session
        if self.boom:
            raise RuntimeError("engine exploded mid-call")
        if self.hold:
            await asyncio.sleep(self.hold)


def roster(n: int) -> list[PoolPersona]:
    return [
        PoolPersona(name=f"Agent{i}", voice=f"voice-{i}", system_prompt=f"prompt {i}")
        for i in range(n)
    ]


class CallLoopTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeEngine.instances = []
        self.config = mock.sentinel.config  # run_call only forwards it
        self.transport = FakeTransport()

    def patch_engine(self, **kwargs):
        """Swap the real engine factory for the fake, per test."""
        return mock.patch.object(
            bot, "create_engine_for_persona",
            side_effect=lambda cfg, persona: FakeEngine(persona, **kwargs),
        )

    async def run_calls(self, pool, sessions, **kwargs):
        with self.patch_engine(**kwargs):
            await asyncio.gather(*(
                bot.run_call(self.config, pool, self.transport, s) for s in sessions
            ))

    # -- concurrency --------------------------------------------------------
    async def test_concurrent_calls_get_different_personas(self):
        pool = AgentPool(roster(3))
        sessions = [FakeSession(f"call-{i}") for i in range(3)]
        # hold > 0 so all three really are in flight at the same moment; without
        # it each call would finish before the next began and any pool would pass.
        await self.run_calls(pool, sessions, hold=0.05)

        assigned = [e.persona.name for e in FakeEngine.instances]
        self.assertEqual(len(assigned), 3)
        self.assertEqual(len(set(assigned)), 3, "two callers got the same agent")
        self.assertEqual(self.transport.rejected, [])

    async def test_every_call_gets_its_own_engine(self):
        """The isolation requirement, at the level this file can check it.

        Cross-contamination between concurrent calls (a shared VAD, a shared
        conversation context) is prevented by never sharing an engine. Here that
        means: N calls, N distinct engine objects, each bound to its own session.
        """
        pool = AgentPool(roster(3))
        sessions = [FakeSession(f"call-{i}") for i in range(3)]
        await self.run_calls(pool, sessions, hold=0.05)

        self.assertEqual(len({id(e) for e in FakeEngine.instances}), 3)
        self.assertEqual(
            {e.ran_with.call_id for e in FakeEngine.instances},
            {"call-0", "call-1", "call-2"},
        )

    async def test_caller_beyond_capacity_is_rejected_not_answered(self):
        pool = AgentPool(roster(2))
        sessions = [FakeSession(f"call-{i}") for i in range(3)]
        await self.run_calls(pool, sessions, hold=0.05)

        self.assertEqual(len(FakeEngine.instances), 2, "over-capacity call was answered")
        self.assertEqual(len(self.transport.rejected), 1)
        # Rejected callers are hung up on, not left connected to silence.
        rejected_id = self.transport.rejected[0]
        rejected = next(s for s in sessions if s.call_id == rejected_id)
        self.assertEqual(rejected.hangups, 1)

    # -- the persona always comes back --------------------------------------
    async def test_persona_released_after_a_normal_call(self):
        pool = AgentPool(roster(2))
        await self.run_calls(pool, [FakeSession("call-1")])
        self.assertEqual(pool.stats().free, 2)

    async def test_persona_released_when_the_engine_raises(self):
        """An engine crash must not cost an agent permanently."""
        pool = AgentPool(roster(2))
        with mock.patch.object(bot.logger, "exception") as logged:
            await self.run_calls(pool, [FakeSession("call-1")], boom=True)
        self.assertEqual(pool.stats().free, 2, "agent leaked on engine failure")
        logged.assert_called_once()

    async def test_persona_released_when_engine_CONSTRUCTION_fails(self):
        """The subtle one: taken from the pool, then never successfully built.

        A `try` that starts after the engine is constructed would leak here --
        the agent is already out of the pool when the constructor throws.
        """
        pool = AgentPool(roster(2))
        session = FakeSession("call-1")
        with mock.patch.object(
            bot, "create_engine_for_persona", side_effect=RuntimeError("bad voice")
        ):
            with mock.patch.object(bot.logger, "exception"):
                await bot.run_call(self.config, pool, self.transport, session)

        self.assertEqual(pool.stats().free, 2, "agent leaked on construction failure")
        self.assertEqual(session.hangups, 1, "call was left up")

    async def test_persona_released_when_the_call_task_is_cancelled(self):
        """A dropped call arrives as task cancellation. The agent still returns."""
        pool = AgentPool(roster(2))
        session = FakeSession("call-1")
        with self.patch_engine(hold=10):
            task = asyncio.create_task(
                bot.run_call(self.config, pool, self.transport, session)
            )
            await asyncio.sleep(0.05)          # let it acquire and start
            self.assertEqual(pool.stats().busy, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(pool.stats().free, 2, "agent leaked on a cancelled call")

    async def test_hangup_runs_even_when_the_engine_raises(self):
        pool = AgentPool(roster(1))
        session = FakeSession("call-1")
        with mock.patch.object(bot.logger, "exception"):
            await self.run_calls(pool, [session], boom=True)
        self.assertEqual(session.hangups, 1)

    # -- reuse --------------------------------------------------------------
    async def test_a_released_persona_serves_the_next_caller(self):
        """Six sequential calls over a roster of three: nothing leaks, and the
        pool is exactly as it started."""
        pool = AgentPool(roster(3))
        for i in range(6):
            await self.run_calls(pool, [FakeSession(f"call-{i}")])
            self.assertEqual(pool.stats().free, 3)

        self.assertEqual(len(FakeEngine.instances), 6)
        # Six calls, six engines -- an engine is never reused, even when the
        # persona is. That is what makes the next caller's context clean.
        self.assertEqual(len({id(e) for e in FakeEngine.instances}), 6)

    async def test_pool_recovers_after_being_full(self):
        pool = AgentPool(roster(1))
        await self.run_calls(pool, [FakeSession("a"), FakeSession("b")], hold=0.05)
        self.assertEqual(len(self.transport.rejected), 1)

        # The next caller, after the rush, is served normally.
        self.transport.rejected.clear()
        await self.run_calls(pool, [FakeSession("c")])
        self.assertEqual(self.transport.rejected, [])
        self.assertEqual(pool.stats().free, 1)


if __name__ == "__main__":
    unittest.main()
