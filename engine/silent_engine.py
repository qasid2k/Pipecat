"""An engine that answers and says nothing. For load testing.

WHY THIS EXISTS
---------------
Stage E has to find the single-node call ceiling, and the honest way to do that
is to run many concurrent calls and see what breaks. Doing that through the real
engine would open N Deepgram streams and N Gemini streams per run -- real money,
and it would measure *provider* limits rather than *this machine's* limits. The
two are different questions and both matter, so they should be asked separately:

  * `engine.provider: silent`   -- what can this VM's event loop, threads and
    sockets sustain?
  * `engine.provider: pipecat` -- what can the accounts sustain?

WHAT IT DOES AND DOES NOT EXERCISE
----------------------------------
Exercised, because these are what the ceiling is made of: the accept path, the
per-call task, UUID correlation, the pool, the two I/O threads per call, the
20 ms write pacing and its back-pressure, the record write, and teardown.

Not exercised: STT, the LLM, TTS, and the VAD. So a number from this run is an
**upper bound on the transport layer**, not a prediction of real capacity. Say
so whenever quoting it.

It writes a frame for every frame it receives, which is *more* outbound audio
than a real call produces -- a real agent speaks maybe a third of the time. That
is deliberate: it loads the write path harder than production does, so the
number errs toward caution.

THE SECOND ENGINE
-----------------
[[roadmap]] lists "a second Engine implementation" as deliberately not built,
with the trigger "a real requirement appears". Load testing without provider
cost is that requirement. It is a modest one -- this proves the `Engine` seam is
*usable*, not that a full alternative conversation stack would drop in -- so the
honest caveat in [[decisions]] stands: whole-engine portability is still
designed for rather than demonstrated.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from core.engine import Engine, EngineResult
from core.transport import CANONICAL_FRAME_BYTES, CallSession

SILENCE = b"\x00" * CANONICAL_FRAME_BYTES


class SilentEngine(Engine):
    """Reads the caller's audio, returns silence, ends when they hang up."""

    def __init__(self, echo: bool = False):
        # echo=True sends the caller's own audio back, which makes a manual test
        # call audible ("can you hear yourself?") and proves the round trip
        # without any provider. Off by default: silence is the realistic load.
        self._echo = echo

    async def run(self, session: CallSession) -> EngineResult:
        frames_in = 0
        cause = "caller hung up"
        try:
            while True:
                frame = await session.read_audio()
                if frame is None:
                    # The one-and-only end-of-call sentinel, per the CallSession
                    # contract. Anything else here would spin forever.
                    break
                frames_in += 1
                # Awaited, not fired and forgotten: this is where the outbound
                # back-pressure applies, and skipping it would make the load test
                # measure a pacing path that production does not use.
                await session.write_audio(frame if self._echo else SILENCE)
        except asyncio.CancelledError:
            cause = "cancelled (shutdown drain)"
            raise
        except Exception as e:  # noqa: BLE001
            cause = f"silent engine failed: {e!r}"
            logger.exception(f"silent engine failed: {e}")
        finally:
            logger.info(f"silent engine done: {frames_in} frames in")

        return EngineResult(cause=cause, turns=0)
