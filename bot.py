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

from core.config import AppConfig, ConfigError, PoolPersona, load_config
from core.engine import EngineResult
from core.live import Counters, LiveCall, LiveCalls
from core.logging import configure_logging
from core.pool import AgentPool
from core.records import CallRecord, RecordWriter, utcnow
from core.transport import BaseTransport, CallSession
from factories import create_call_store, create_engine_for_persona, create_transport

load_dotenv()

# Keeps a reference to every in-flight call task. Without this, CPython is free
# to garbage-collect a running task that nothing else holds.
_active_calls: set[asyncio.Task] = set()


def _call_record(
    config: AppConfig,
    session: CallSession,
    persona: PoolPersona,
    result: EngineResult | None,
    started_at,
    ended_at,
) -> CallRecord:
    """Assemble one call's row from the three places that know about it.

    Nothing here computes or guesses: the transport supplies the identifiers and
    the frame counters, the pool supplies who took the call, and the engine
    supplies what only it saw. `result` is None when the engine crashed before
    returning — the row is still written, because a call that failed is a call
    worth having a record of, arguably more than one that went fine.
    """
    io_stats = session.io_counters()
    vendor = session.vendor_ids
    return CallRecord(
        call_id=str(session.call_id),
        tenant_id=config.service.tenant_id,
        started_at=started_at,
        ended_at=ended_at,
        duration_s=round((ended_at - started_at).total_seconds(), 3),
        caller_id=session.caller_id,
        uniqueid=vendor.get("uniqueid", ""),
        linkedid=vendor.get("linkedid", ""),
        persona=persona.name,
        voice=persona.voice,
        llm_model=config.engine.llm.model,
        end_reason=session.end_reason,
        cause=result.cause if result else "engine failed before it could report",
        transferred_to=result.transferred_to if result else None,
        transcript_path=result.transcript_path if result else "",
        conversation_path=result.conversation_path if result else "",
        **io_stats,
    )


async def run_call(
    config: AppConfig,
    pool: AgentPool,
    transport: BaseTransport,
    session: CallSession,
    records: RecordWriter | None = None,
    live: LiveCalls | None = None,
    counters: Counters | None = None,
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
    # Everything logged inside this block -- here, in the engine, in the
    # transcript recorder, in code that has never heard of logging setup --
    # carries this call's id. contextualize() stores it in a contextvar, and
    # each call is its own task, so the bindings cannot bleed between concurrent
    # calls. The AudioSocket I/O threads are the one exception: threads do not
    # inherit the context, so they bind explicitly (see core/logging.py).
    with logger.contextualize(call_id=session.call_id, caller_id=session.caller_id):
        await _serve(config, pool, transport, session, records, live, counters)


async def _serve(
    config: AppConfig,
    pool: AgentPool,
    transport: BaseTransport,
    session: CallSession,
    records: RecordWriter | None = None,
    live: LiveCalls | None = None,
    counters: Counters | None = None,
):
    persona = await pool.acquire()

    if persona is None:
        # The app-side capacity gate. Transport-agnostic, and the only gate that
        # exists for a vendor with no dialplan. On Asterisk this is the safety
        # net behind the dialplan's spoken "all agents busy" message (Phase 4);
        # reaching it means the caller is hung up on without explanation, so a
        # rise in these lines means the dialplan cap and the roster have drifted.
        # No [call_id] prefix any more: the logging setup binds it to every line
        # in this call, so repeating it in the message would just print it twice.
        logger.warning(
            f"POOL FULL -- rejecting call from {session.caller_id} | {pool.stats()}"
        )
        if counters is not None:
            counters.calls_rejected_total += 1
        await transport.reject(session)
        return

    logger.info(
        f"assigned '{persona.name}' ({persona.voice}) to {session.caller_id} "
        f"| {pool.stats()}"
    )
    if counters is not None:
        counters.calls_total += 1
    if live is not None:
        live.started(
            LiveCall(
                call_id=str(session.call_id),
                caller_id=session.caller_id,
                persona=persona.name,
                voice=persona.voice,
                started_at=utcnow(),
            )
        )
    started_at = utcnow()
    result: EngineResult | None = None
    try:
        engine = create_engine_for_persona(config, persona, records=records)
        result = await engine.run(session)
    except Exception as e:
        logger.exception(f"engine failed ('{persona.name}'): {e}")
        if counters is not None:
            counters.calls_failed_total += 1
    finally:
        # Off the live list FIRST, before anything that can be slow or fail.
        # A call still showing as in-progress after it ended is the one way this
        # display can actively mislead, and the fix must not depend on the rest
        # of the teardown succeeding.
        if live is not None:
            live.ended(str(session.call_id))
        # Safe on a CANCELLED call (a dropped line, and in Phase 5 a shutdown
        # drain) only because `release` never yields: its lock is always
        # uncontended, since nothing awaits inside the critical section. If that
        # ever changes, cancellation could interrupt this line and leak the
        # agent -- the same `await`-in-the-lock hazard the lock guards against.
        await pool.release(persona)
        logger.info(
            f"released '{persona.name}' ({session.end_reason}) | {pool.stats()}"
        )
        # disconnect(), not hangup(). Reaching here means the conversation is
        # over from OUR side -- the caller hung up, or the engine finished, or
        # the idle timeout fired, or a shutdown cancelled us. hangup() alone only
        # closes our audio path, which is right when the caller ended the call
        # but abandons them when we did: still connected, hearing silence, their
        # channel held open and (on Asterisk) still holding a capacity slot.
        # disconnect() hangs up the caller too, and knows to skip that when they
        # already left or were transferred to a human.
        await session.disconnect()
        # Written LAST, once the call is genuinely over, so the row carries the
        # final frame counters and end reason rather than a snapshot from
        # halfway through. submit() never blocks and never raises, so this
        # cannot delay the next caller or mask an error on the way out.
        record = _call_record(config, session, persona, result, started_at, utcnow())
        if records is not None:
            records.submit(record)
        if counters is not None:
            # Summed from the finished call, so the totals and the row agree.
            counters.frames_dropped_total += record.frames_dropped
            counters.pacer_slips_total += record.pacer_slips
            if record.transferred_to:
                counters.transfers_total += 1


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

    # Records: one writer for the whole service, draining to one store. Started
    # BEFORE the transport so no call can be answered before there is somewhere
    # to record it. A store that cannot even open is a startup failure, not a
    # surprise on the first call.
    store = create_call_store(config)
    records = RecordWriter(store)
    records.set_logger(logger)
    await records.start()
    logger.info(f"Records: {store.describe}")

    # Observation state. Separate from the pool on purpose: the pool decides who
    # may answer, these only watch. See core/live.py.
    live = LiveCalls()
    counters = Counters()

    transport = create_transport(config)
    await transport.start()

    # The API comes up AFTER the transport, so /health cannot report ready while
    # the thing that answers calls is still binding its socket.
    api = None
    if config.service.api.enabled:
        from api.server import ApiServer

        api = ApiServer(
            pool=pool, live=live, counters=counters, records=records,
            host=config.service.api.host, port=config.service.api.port,
            tenant_id=config.service.tenant_id,
            engine_provider=config.engine.provider,
        )
        try:
            await api.start()
        except OSError as e:
            # The API only observes. Refusing to answer phone calls because a
            # monitoring port is taken -- by a stale process, or an SSH tunnel
            # someone left open -- would be the observation breaking the thing
            # observed, which is the one thing it must never do. Same rule as
            # the record writer and the live-call registry.
            logger.error(
                f"API could not start on {config.service.api.host}:"
                f"{config.service.api.port} ({e}). Continuing WITHOUT it -- "
                "no dashboard and no /metrics until the port is free."
            )
            api = None

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
                logger.warning(
                    f"shutting down -- refusing call {session.call_id}"
                )
                await transport.reject(session)
                continue
            # One TASK per call, deliberately: awaiting run_call here would run
            # calls one at a time and destroy concurrency. The pool is acquired
            # INSIDE the task for the same reason -- rejecting a call can mean
            # talking to the vendor, and doing that here would stall the next
            # caller behind it.
            task = asyncio.create_task(
                run_call(
                    config, pool, transport, session, records, live, counters
                )
            )
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
        # The API goes first: it only reports, and leaving it up during the
        # drain would answer /health as healthy while the service is on its way
        # out -- which is exactly when a load balancer must stop sending traffic.
        if api is not None:
            await api.stop()
        await _drain(config.service.drain_timeout_s, force)
        accepting.cancel()
        # After the drain, because the calls that just ended submit their rows
        # on the way out and those are exactly the ones worth keeping. close()
        # waits for the queue, time-boxed so a hung store cannot hold up a
        # shutdown.
        await records.close()
        logger.info(f"Records: {records.stats}")
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

    # Sinks first: everything below this line is meant to land in them, and a
    # log line written before setup goes to loguru's default stderr sink and
    # nowhere else.
    configure_logging(
        level=cfg.service.log.level,
        file=cfg.service.log.file,
        rotation=cfg.service.log.rotation,
        retention=cfg.service.log.retention,
        tenant_id=cfg.service.tenant_id,
        base_dir=cfg.source.parent if cfg.source else None,
    )

    logger.info(f"Config: {cfg.source} | tenant={cfg.service.tenant_id}")
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
