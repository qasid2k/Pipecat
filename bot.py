"""
The voice agent's entry point.

This file is deliberately almost empty: get a transport, get a pool of agents,
and give each incoming call a free agent. It imports no Pipecat, opens no socket
and makes no ARI call.

    config.yaml               WHAT to run: vendor, providers, model, the roster
    core/config.py            the typed, validated loader for it
    core/pool.py              who is free, who is on a call (the capacity gate)
    core/transport.py         CallSession + BaseTransport contracts (vendor-neutral)
    core/engine.py            Engine contract (engine-neutral)
    factories.py              turns config into the actual objects
    transports/asterisk.py    Asterisk/FreePBX: ARI + AudioSocket + transfer
    transports/audiosocket.py the AudioSocket protocol + its I/O threads
    engine/pipecat_engine.py  the Pipecat conversation: STT -> LLM -> TTS + tools
    bot.py (this file)        wiring

There is exactly ONE way a call is handled: `run_call`. Capacity is the size of
the roster in config.yaml, and a caller who arrives when every agent is busy is
refused rather than answered badly.

Switching telephony vendor, swapping an STT/LLM/TTS provider, adding an agent or
changing a voice, prompt or the turn-taking timing are all config.yaml edits.
Secrets are never in that file -- it names environment variables, .env holds them.

    python bot.py [path/to/config.yaml]     # or set VOICEAGENT_CONFIG
"""

import asyncio
import sys

from dotenv import load_dotenv
from loguru import logger

from core.config import AppConfig, ConfigError, load_config
from core.pool import AgentPool
from core.transport import BaseTransport, CallSession
from factories import create_engine_for_persona, create_transport

load_dotenv()

# Keeps a reference to every in-flight call task. Without this, CPython is free
# to garbage-collect a running task that nothing else holds.
_active_calls: set[asyncio.Task] = set()


async def run_call(
    config: AppConfig, pool: AgentPool, transport: BaseTransport, session: CallSession
):
    """One call, start to finish. The ONLY path a call can take.

    Take a free agent, build that agent a brand-new engine, talk, give the agent
    back. If nobody is free, refuse the call rather than answering badly.

    WHY THE ENGINE IS BUILT HERE, PER CALL
    --------------------------------------
    Every call gets its own engine, and with it its own VAD, its own Deepgram
    and Gemini connections, its own audio buffers and its own conversation
    context. None of it is shared or reused. Two things depend on that:

      * concurrent calls cannot contaminate each other -- a shared VAD would
        have callers cutting each other off, a shared LLM context would have
        them reading each other's conversation;
      * a persona reused by the NEXT caller starts blank. The persona itself is
        a frozen description (name, voice, instructions) carrying no history, so
        there is nothing to carry over. That is a privacy guarantee, not a
        performance detail -- caching an engine per persona to save startup time
        would quietly break it.

    WHY THE `finally`
    -----------------
    The persona goes back to the pool on EVERY exit path: normal hangup, caller
    dropping the line, the engine raising, or `create_engine_for_persona` itself
    failing after the agent was already taken. Miss any one of those and that
    agent is busy forever -- capacity drops by one, permanently, with nothing in
    the logs to say why. Release comes BEFORE hangup deliberately: hangup can
    fail (it talks to the vendor), and if it did, an agent released afterwards
    would never be released at all.
    """
    persona = await pool.acquire()

    if persona is None:
        # The app-side capacity gate. Transport-agnostic, and the only gate that
        # exists for a vendor with no dialplan. On Asterisk this is the safety
        # net behind the dialplan's spoken "all agents busy" message (Phase 4);
        # reaching it means the caller is hung up on without explanation, so a
        # rise in these lines means the dialplan cap and the roster have drifted.
        logger.warning(
            f"[{session.call_id}] POOL FULL -- rejecting call from "
            f"{session.caller_id} | {pool.stats()}"
        )
        await transport.reject(session)
        return

    logger.info(
        f"[{session.call_id}] assigned '{persona.name}' ({persona.voice}) to "
        f"{session.caller_id} | {pool.stats()}"
    )
    try:
        engine = create_engine_for_persona(config, persona)
        await engine.run(session)
    except Exception as e:
        logger.exception(f"[{session.call_id}] engine failed ('{persona.name}'): {e}")
    finally:
        # Safe on a CANCELLED call (a dropped line, and in Phase 5 a shutdown
        # drain) only because `release` never yields: its lock is always
        # uncontended, since nothing awaits inside the critical section. If that
        # ever changes, cancellation could interrupt this line and leak the
        # agent -- the same `await`-in-the-lock hazard the lock guards against.
        await pool.release(persona)
        logger.info(
            f"[{session.call_id}] released '{persona.name}' "
            f"({session.end_reason}) | {pool.stats()}"
        )
        # For Asterisk, hangup() closes the audio path only. It deliberately does
        # NOT hang up the caller's channel, which after a transfer may be talking
        # to a human -- see AsteriskCallSession.hangup.
        await session.hangup()


async def main(config: AppConfig):
    # Built once, shared by every call: the pool is the one piece of state that
    # is SUPPOSED to be global, because "who is busy" is a fact about the whole
    # service. Everything else per call is per call.
    pool = AgentPool(config.pool.personas)

    transport = create_transport(config)
    await transport.start()

    try:
        async for session in transport.listen():
            # One TASK per call, deliberately: awaiting run_call here would run
            # calls one at a time and destroy concurrency. The pool is acquired
            # INSIDE the task for the same reason -- rejecting a call can mean
            # talking to the vendor, and doing that here would stall the next
            # caller behind it.
            task = asyncio.create_task(run_call(config, pool, transport, session))
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

    logger.info(f"Config: {cfg.source}")
    logger.info(
        f"transport={cfg.transport.provider} | engine={cfg.engine.provider} | "
        f"{cfg.engine.stt.provider} STT -> {cfg.engine.llm.model} -> "
        f"{cfg.engine.tts.provider} TTS"
    )
    # Capacity is printed at startup because it is the number that has to match
    # the Asterisk dialplan's GROUP cap, which cannot read this file. If a call
    # is ever rejected unexpectedly, this line is the first thing to check.
    logger.info(f"Pool: capacity {cfg.pool.capacity} (max simultaneous calls)")
    for p in cfg.pool.personas:
        logger.info(f"  - {p.name} ({p.voice})")

    try:
        asyncio.run(main(cfg))
    except KeyboardInterrupt:
        logger.info("Shutting down.")
