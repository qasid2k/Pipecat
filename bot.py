"""
The voice agent's entry point.

After Phases 2 and 3 this file is deliberately almost empty: get a transport,
get an engine, introduce them to each other. It imports no Pipecat, opens no
socket and makes no ARI call.

    core/transport.py         CallSession + BaseTransport contracts (vendor-neutral)
    core/engine.py            Engine contract (engine-neutral)
    transports/asterisk.py    Asterisk/FreePBX: ARI + AudioSocket + transfer
    transports/audiosocket.py the AudioSocket protocol + its I/O threads
    engine/pipecat_engine.py  the Pipecat conversation: STT -> LLM -> TTS + tools
    bot.py (this file)        wiring

Phase 4 replaces the two "which one?" decisions below with config.yaml lookups,
so switching transport or engine needs no code change at all.
"""

import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger

from core.engine import Engine
from core.transport import CallSession
from engine.pipecat_engine import PipecatEngine
from transports.asterisk import transport_from_env

load_dotenv()

# Keeps a reference to every in-flight call task. Without this, CPython is free
# to garbage-collect a running task that nothing else holds.
_active_calls: set[asyncio.Task] = set()


def create_engine() -> Engine:
    """Build the conversation engine for one call.

    Phase 4 turns this into create_engine(config), selecting the engine by name.
    A new engine per call because each holds that call's conversation state.
    """
    return PipecatEngine()


async def run_call(session: CallSession):
    """One call: hand it to an engine, and guarantee cleanup afterwards.

    The `finally` is the point. Whether the conversation ended normally, the
    caller hung up, or the engine raised, the session is released exactly once
    on the way out -- the engine never has to remember to do it.

    (For Asterisk, hangup() closes the audio path only. It deliberately does NOT
    hang up the caller's channel, which after a transfer may be talking to a
    human -- see AsteriskCallSession.hangup.)
    """
    engine = create_engine()
    try:
        await engine.run(session)
    except Exception as e:
        logger.exception(f"Engine failed on call {session.call_id}: {e}")
    finally:
        await session.hangup()


def check_keys():
    """Fail loudly at startup rather than mysteriously mid-call."""
    missing = [k for k in ("DEEPGRAM_API_KEY", "GEMINI_API_KEY") if not os.getenv(k)]
    if missing:
        logger.error(f"Missing in .env: {', '.join(missing)}")
        logger.error("Open the .env file and paste your keys after the '=' signs.")
        sys.exit(1)


async def main():
    check_keys()

    transport = transport_from_env()  # Phase 4: create_transport(config)
    await transport.start()

    try:
        async for session in transport.listen():
            # One TASK per call, deliberately: awaiting run_call here would run
            # calls one at a time and destroy concurrency.
            task = asyncio.create_task(run_call(session))
            _active_calls.add(task)
            task.add_done_callback(_active_calls.discard)
    finally:
        await transport.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
