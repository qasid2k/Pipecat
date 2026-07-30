"""
The voice agent's entry point.

After Phases 2 and 3 this file is deliberately almost empty: get a transport,
get an engine, introduce them to each other. It imports no Pipecat, opens no
socket and makes no ARI call.

    config.yaml               WHAT to run: vendor, providers, model, voice, persona
    core/config.py            the typed, validated loader for it
    core/transport.py         CallSession + BaseTransport contracts (vendor-neutral)
    core/engine.py            Engine contract (engine-neutral)
    factories.py              turns config into the actual objects
    transports/asterisk.py    Asterisk/FreePBX: ARI + AudioSocket + transfer
    transports/audiosocket.py the AudioSocket protocol + its I/O threads
    engine/pipecat_engine.py  the Pipecat conversation: STT -> LLM -> TTS + tools
    bot.py (this file)        wiring

Switching telephony vendor, swapping an STT/LLM/TTS provider, changing the
voice, the persona or the turn-taking timing are all config.yaml edits. Secrets
are never in that file -- it names environment variables, and .env holds them.

    python bot.py [path/to/config.yaml]     # or set VOICEAGENT_CONFIG
"""

import asyncio
import sys

from dotenv import load_dotenv
from loguru import logger

from core.config import AppConfig, ConfigError, load_config
from core.transport import CallSession
from factories import create_engine, create_transport

load_dotenv()

# Keeps a reference to every in-flight call task. Without this, CPython is free
# to garbage-collect a running task that nothing else holds.
_active_calls: set[asyncio.Task] = set()


async def run_call(config: AppConfig, session: CallSession):
    """One call: hand it to an engine, and guarantee cleanup afterwards.

    The `finally` is the point. Whether the conversation ended normally, the
    caller hung up, or the engine raised, the session is released exactly once
    on the way out -- the engine never has to remember to do it.

    (For Asterisk, hangup() closes the audio path only. It deliberately does NOT
    hang up the caller's channel, which after a transfer may be talking to a
    human -- see AsteriskCallSession.hangup.)
    """
    engine = create_engine(config)
    try:
        await engine.run(session)
    except Exception as e:
        logger.exception(f"Engine failed on call {session.call_id}: {e}")
    finally:
        await session.hangup()


async def main(config: AppConfig):
    transport = create_transport(config)
    await transport.start()

    try:
        async for session in transport.listen():
            # One TASK per call, deliberately: awaiting run_call here would run
            # calls one at a time and destroy concurrency.
            task = asyncio.create_task(run_call(config, session))
            _active_calls.add(task)
            task.add_done_callback(_active_calls.discard)
    finally:
        await transport.stop()


if __name__ == "__main__":
    # Config is loaded and fully validated BEFORE anything starts listening, so
    # a bad setting is a startup error with a clear message rather than a
    # surprise on the first call.
    try:
        cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else None)
    except ConfigError as e:
        logger.error(f"Configuration problem:\n{e}")
        sys.exit(1)

    p = cfg.engine.persona
    logger.info(f"Config: {cfg.source}")
    logger.info(
        f"Agent '{p.name}' for {p.company} | transport={cfg.transport.provider} "
        f"| engine={cfg.engine.provider} "
        f"| {cfg.engine.stt.provider} STT -> {cfg.engine.llm.model} -> "
        f"{cfg.engine.tts.voice}"
    )

    try:
        asyncio.run(main(cfg))
    except KeyboardInterrupt:
        logger.info("Shutting down.")
