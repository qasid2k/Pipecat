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
import signal
import sys
import time

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


def _install_signal_handlers(stop: asyncio.Event, force: asyncio.Event) -> None:
    """Turn SIGINT/SIGTERM into events the shutdown sequence can wait on.

    FIRST signal  -> stop accepting new calls and let the ones in progress end.
    SECOND signal -> stop waiting and cancel them now.

    The second one matters as much as the first. An operator who has already
    asked twice should not be told to wait; without it, a single caller who
    never hangs up holds a deploy hostage for the whole drain timeout.

    Windows needs the fallback. `loop.add_signal_handler` is not implemented on
    the proactor loop, and this project is developed on Windows even though it
    runs on Linux -- so the path that raises is the one the developer hits.
    `signal.signal` runs its handler on the main thread outside the loop, hence
    `call_soon_threadsafe` to get back onto it.
    """
    loop = asyncio.get_running_loop()

    def request_stop(signame: str) -> None:
        if stop.is_set():
            logger.warning(f"{signame} again -- cutting in-flight calls off now")
            force.set()
        else:
            logger.info(f"{signame} received -- draining (Ctrl+C again to force)")
            stop.set()

    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:  # SIGTERM does not exist on some platforms
            continue
        try:
            loop.add_signal_handler(sig, request_stop, signame)
        except NotImplementedError:
            signal.signal(
                sig,
                lambda _s, _f, name=signame: loop.call_soon_threadsafe(
                    request_stop, name
                ),
            )


async def _drain(timeout: float, force: asyncio.Event) -> None:
    """Wait for in-flight calls to finish, then cut off whatever is left.

    Cancelling is safe rather than brutal: every call's `finally` still runs, so
    the persona goes back to the pool and the audio path is closed. What a
    cancelled caller loses is the rest of their sentence, not their slot.
    """
    calls = set(_active_calls)  # a copy: done-callbacks mutate the live set
    if not calls:
        logger.info("no calls in progress")
        return

    logger.info(f"waiting up to {timeout:.0f}s for {len(calls)} call(s) to finish")
    finished = asyncio.create_task(asyncio.wait(calls))
    forced = asyncio.create_task(force.wait())
    try:
        await asyncio.wait(
            {finished, forced}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        forced.cancel()

    still_up = [t for t in calls if not t.done()]
    if still_up:
        why = "forced" if force.is_set() else f"still up after {timeout:.0f}s"
        logger.warning(f"cancelling {len(still_up)} call(s) -- {why}")
        for task in still_up:
            task.cancel()
        # Wait for the cancellations to actually land, so the personas are
        # released and the sockets closed BEFORE the transport is torn down.
        await asyncio.gather(*still_up, return_exceptions=True)
    finished.cancel()

    # Collect every result, including from calls that finished on their own.
    # Not politeness: an exception nobody retrieves is re-raised by asyncio when
    # the task is garbage-collected, so a call that failed on the way out would
    # print a stray traceback in the middle of the shutdown log -- exactly where
    # it looks like the shutdown itself broke.
    await asyncio.gather(*calls, return_exceptions=True)


async def main(config: AppConfig):
    # Built once, shared by every call: the pool is the one piece of state that
    # is SUPPOSED to be global, because "who is busy" is a fact about the whole
    # service. Everything else per call is per call.
    pool = AgentPool(config.pool.personas)

    # Pay the engine's import cost NOW, at startup, with nobody on the line.
    #
    # `create_engine` imports engine/pipecat_engine.py lazily, and that module
    # pulls in onnxruntime (Silero VAD), google-genai/grpc and the Deepgram SDK.
    # That import is synchronous, so it blocks the whole event loop, and cold on
    # the VM it was observed blocking it for THIRTY-SEVEN SECONDS (see bugs.md
    # B-011). Left until the first call, it blocks the loop *during* that call: the caller hears only the
    # write thread's silence keep-alive, ARI's own events sit unprocessed until
    # they time out, and by the time the loop breathes again the caller has hung
    # up and the media channel is gone. The first caller after every restart
    # loses their call; everyone after them is fine, which is exactly the kind
    # of bug that survives testing.
    #
    # Building one throwaway engine here does the import and proves the
    # configured engine can actually be constructed, before anything is
    # listening. PipecatEngine.__init__ only stores config, so this opens no
    # connection and starts no pipeline.
    warm_start = time.monotonic()
    create_engine_for_persona(config, config.pool.personas[0])
    logger.info(f"Engine ready ({time.monotonic() - warm_start:.1f}s warm-up)")

    transport = create_transport(config)
    await transport.start()

    stop = asyncio.Event()
    force = asyncio.Event()
    _install_signal_handlers(stop, force)

    async def accept_calls():
        async for session in transport.listen():
            if stop.is_set():
                # Arrived mid-shutdown. Refuse it rather than answer a call this
                # process is about to stop serving -- a caller told "no" can ring
                # back, a caller answered and then cut off cannot tell what
                # happened. Asterisk will keep connecting until the transport is
                # actually torn down, so this branch is reachable in practice.
                logger.warning(f"[{session.call_id}] shutting down -- refusing call")
                await transport.reject(session)
                continue
            # One TASK per call, deliberately: awaiting run_call here would run
            # calls one at a time and destroy concurrency. The pool is acquired
            # INSIDE the task for the same reason -- rejecting a call can mean
            # talking to the vendor, and doing that here would stall the next
            # caller behind it.
            task = asyncio.create_task(run_call(config, pool, transport, session))
            _active_calls.add(task)
            task.add_done_callback(_active_calls.discard)

    accepting = asyncio.create_task(accept_calls())
    stopping = asyncio.create_task(stop.wait())
    try:
        # Park here until a signal arrives, or until the accept loop falls over
        # on its own -- if it dies, nothing is answering calls any more and the
        # process should stop rather than sit there looking healthy.
        await asyncio.wait(
            {accepting, stopping}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        stopping.cancel()
        # ORDER MATTERS. Drain the calls FIRST, while the transport is still up:
        # ending a call needs ARI (to tear down its bridge and media channel) and
        # the audio path (to close cleanly). Stopping the transport first would
        # pull both out from under the very calls we are trying to end politely,
        # and leave orphaned channels on the Asterisk side.
        logger.info("shutting down: no new calls will be accepted")
        await _drain(config.service.drain_timeout_s, force)
        accepting.cancel()
        await transport.stop()
        logger.info(f"stopped cleanly | {pool.stats()}")


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
