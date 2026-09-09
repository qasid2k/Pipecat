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
from core.config import AppConfig, EngineConfig, PoolPersona, TransportConfig
from core.engine import EngineResult
from core.pool import AgentPool
from core.records import CallStore, RecordWriter
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
        # A real AppConfig on defaults, not a sentinel: run_call no longer just
        # forwards this -- it reads tenant_id and the model name to build the
        # call record. Every field here has a dataclass default, so no YAML and
        # no environment variables are needed.
        self.config = AppConfig(transport=TransportConfig(), engine=EngineConfig())
        self.transport = FakeTransport()

    def patch_engine(self, **kwargs):
        """Swap the real engine factory for the fake, per test."""
        return mock.patch.object(
            bot, "create_engine_for_persona",
            # **_ absorbs `records=`: this fake stands in for the factory, and a
            # test should not fail because the factory grew an argument it does
            # not care about.
            side_effect=lambda cfg, persona, **_: FakeEngine(persona, **kwargs),
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


class CallRecordWiringTest(CallLoopTest):
    """Stage C part 2: every finished call leaves a row.

    The row is written in the `finally`, after the persona is released and the
    caller disconnected, so it carries the final counters rather than a snapshot
    from halfway through -- and so it is written on the paths that matter most,
    which are the ones where something went wrong.
    """

    def setUp(self):
        super().setUp()
        self.store = _CollectingStore()
        self.records = RecordWriter(self.store)

    async def run_with_records(self, pool, sessions, **kwargs):
        await self.records.start()
        with self.patch_engine(**kwargs):
            await asyncio.gather(*(
                bot.run_call(self.config, pool, self.transport, s, self.records)
                for s in sessions
            ))
        await self.records.close()

    async def test_a_normal_call_is_recorded(self):
        pool = AgentPool(roster(2))
        session = FakeSession("call-1")
        session.end_reason = "caller hung up"
        await self.run_with_records(pool, [session])

        self.assertEqual(len(self.store.calls), 1)
        row = self.store.calls[0]
        self.assertEqual(row.call_id, "call-1")
        self.assertEqual(row.caller_id, "+10000000000")
        self.assertEqual(row.end_reason, "caller hung up")
        self.assertIn(row.persona, {"Agent0", "Agent1"})
        self.assertGreaterEqual(row.duration_s, 0)

    async def test_a_crashed_engine_is_still_recorded(self):
        """The most important row to have. A call that failed is worth a record
        arguably more than one that went fine, and `result` being None must not
        skip the write."""
        pool = AgentPool(roster(2))
        with mock.patch.object(bot.logger, "exception"):
            await self.run_with_records(pool, [FakeSession("call-1")], boom=True)

        self.assertEqual(len(self.store.calls), 1)
        self.assertIn("engine failed", self.store.calls[0].cause)

    async def test_a_rejected_caller_is_not_recorded_as_a_call(self):
        """They were never served, so there is no call to describe. Counting
        turned-away callers is a CDR question -- see decisions 041."""
        pool = AgentPool(roster(1))
        await self.run_with_records(
            pool, [FakeSession("a"), FakeSession("b")], hold=0.05
        )
        self.assertEqual(len(self.transport.rejected), 1)
        self.assertEqual(len(self.store.calls), 1)

    async def test_the_engine_result_reaches_the_row(self):
        """cause and transferred_to are visible only from inside the engine."""
        pool = AgentPool(roster(1))
        await self.records.start()
        with mock.patch.object(
            bot, "create_engine_for_persona",
            side_effect=lambda cfg, persona, **_: _ReportingEngine(),
        ):
            await bot.run_call(
                self.config, pool, self.transport, FakeSession("call-1"),
                self.records,
            )
        await self.records.close()

        row = self.store.calls[0]
        self.assertEqual(row.transferred_to, "billing")
        self.assertEqual(row.cause, "call ended -- caller hung up")
        self.assertTrue(row.transcript_path.endswith("-transcript.jsonl"))

    async def test_recording_is_optional(self):
        """`records=None` must be an ordinary call, not a crash."""
        pool = AgentPool(roster(1))
        with self.patch_engine():
            await bot.run_call(
                self.config, pool, self.transport, FakeSession("call-1")
            )
        self.assertEqual(pool.stats().free, 1)

    async def test_a_broken_store_does_not_break_the_call(self):
        """A record is evidence; the caller is real."""
        pool = AgentPool(roster(1))
        self.records = RecordWriter(_ExplodingStore())
        await self.records.start()
        session = FakeSession("call-1")
        with self.patch_engine():
            await bot.run_call(
                self.config, pool, self.transport, session, self.records
            )
        await self.records.close()

        self.assertEqual(pool.stats().free, 1, "the persona leaked")
        self.assertEqual(session.hangups, 1, "the call was not cleaned up")
        self.assertEqual(self.records.stats.failed, 1)


class _CollectingStore(CallStore):
    def __init__(self):
        self.calls: list = []
        self.turns: list = []

    async def start(self):
        pass

    async def save_call(self, record):
        self.calls.append(record)

    async def save_turns(self, turns):
        self.turns.extend(turns)

    async def close(self):
        pass


class _ExplodingStore(_CollectingStore):
    async def save_call(self, record):
        raise RuntimeError("database is down")


class _ReportingEngine:
    """An engine that reports an outcome, the way PipecatEngine does."""

    async def run(self, session):
        return EngineResult(
            cause="call ended -- caller hung up",
            transferred_to="billing",
            transcript_path="/tmp/x-transcript.jsonl",
            conversation_path="/tmp/x-conversation.json",
            turns=4,
        )


if __name__ == "__main__":
    unittest.main()
