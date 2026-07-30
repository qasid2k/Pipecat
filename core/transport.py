"""
Phase 2: the vendor-neutral telephony interfaces.

This is the seam between "how a phone call reaches us" (Asterisk today, maybe
Twilio tomorrow) and "what we do with the call" (the conversation engine).
Nothing in this file knows what Asterisk, ARI or AudioSocket are -- that is the
whole point. An "abstract base class" (ABC) here just means: a contract that
lists the methods an adapter MUST provide. Python refuses to instantiate a
subclass that forgot one, so the contract is enforced at runtime.

Two contracts:

    CallSession    -- ONE live phone call. Read audio, write audio, transfer,
                      hang up. Created by a transport, consumed by the engine.
    BaseTransport  -- ONE vendor connection. Listens for calls and hands out
                      CallSessions.

THE CANONICAL AUDIO FORMAT
--------------------------
Every byte crossing this interface is 8 kHz / 16-bit signed / mono PCM
("slin" in Asterisk's language), in 20 ms = 320-byte frames. That is exactly
what telephony gives us and exactly what Deepgram and Silero VAD accept, so
today NOTHING resamples anywhere.

If a future vendor speaks something else (Twilio: 8 kHz mu-law, base64, over a
WebSocket), the conversion happens INSIDE that adapter. The core and the engine
must never see a vendor-specific format.
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator

import asyncio

# The canonical format, in one place. Adapters and the engine import these
# rather than each hardcoding 8000 and hoping they agree.
CANONICAL_SAMPLE_RATE = 8000
CANONICAL_CHANNELS = 1
CANONICAL_SAMPLE_WIDTH_BYTES = 2  # 16-bit signed
CANONICAL_FRAME_MS = 20
CANONICAL_FRAME_BYTES = 320  # 8000 Hz * 0.02 s * 1 channel * 2 bytes


class CallSession(ABC):
    """One live phone call, with the vendor's details hidden.

    Concrete adapters (e.g. AsteriskCallSession) MUST set the two attributes
    below in their __init__, and implement the four abstract methods.

    Attributes:
        call_id:   Stable identifier for this call, for logs and filenames.
                   Asterisk uses the AudioSocket UUID.
        caller_id: The caller's number if the vendor told us, else "unknown".
                   Never assume it is present or trustworthy -- it is caller-
                   supplied data on most trunks.
        ended:     Set when the call is over, from whichever side ended it.
                   Await it to know when to tear the conversation down.
        end_reason: Human-readable reason, for the closing log line. Populated
                   by the adapter at the moment the call ends.
    """

    call_id: str = "unknown"
    caller_id: str = "unknown"
    ended: asyncio.Event
    end_reason: str = "not ended"

    @abstractmethod
    async def read_audio(self) -> bytes | None:
        """Wait for and return the next frame of CALLER audio.

        Returns 320 bytes of canonical PCM, or **None** exactly once when the
        call has ended -- that None is the "no more audio ever" sentinel and is
        how the engine's read loop knows to stop. Callers must handle it.
        """

    @abstractmethod
    async def write_audio(self, pcm: bytes) -> None:
        """Send one frame of AGENT audio to the caller.

        Expects canonical PCM. This may block (asynchronously) until the vendor
        is ready for more audio -- that back-pressure is deliberate: it is what
        paces the agent's speech to real time. Do not "fix" it by dropping
        frames or buffering without bound.
        """

    @abstractmethod
    async def transfer(self, destination: str) -> bool:
        """Hand this call to `destination`, e.g. "sales" or "human".

        Transfer is a CONTROL operation and belongs here on the transport, not
        in the conversation pipeline -- each vendor does it completely
        differently (Asterisk: back into the dialplan; Twilio: a REST redirect).

        Returns True if the transfer was initiated, False if this call cannot be
        transferred (see `can_transfer`). Note that True means "handed over",
        not "a human answered" -- what happens next is the vendor's business.
        """

    @abstractmethod
    async def hangup(self) -> None:
        """Release this call's resources. MUST be safe to call twice.

        The engine calls this in a `finally`, so it runs on every exit path --
        including after a successful transfer. It therefore must NOT forcibly
        destroy a call we have already handed to someone else.
        """

    @property
    def can_transfer(self) -> bool:
        """Whether transfer() can work on this call, known up front.

        Needed because the agent must tell the caller the truth *before*
        attempting anything. Defaults to True; adapters override it when a
        particular call lacks call control (Asterisk: a direct AudioSocket call
        has no ARI channel to act on).
        """
        return True


class BaseTransport(ABC):
    """One connection to a telephony vendor; the source of CallSessions.

    Lifecycle:

        await transport.start()
        async for session in transport.listen():
            ...                       # one session per incoming call
        await transport.stop()
    """

    @abstractmethod
    async def start(self) -> None:
        """Begin accepting calls (bind sockets, connect to the vendor's API)."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop accepting calls and release vendor resources. Idempotent."""

    @abstractmethod
    def listen(self) -> AsyncIterator[CallSession]:
        """Yield one CallSession per incoming call, as they arrive.

        Implemented as an async generator, so it is used as
        `async for session in transport.listen():` with no `await` on the call
        itself.

        IMPORTANT: an implementation must not do slow per-call setup inline in
        this loop -- that would make call N+1 wait behind call N. Do such work
        in a task and yield the session once it is ready. Audio is already
        flowing the instant the vendor connects; the clock is running.
        """

    @abstractmethod
    async def reject(self, call: CallSession) -> None:
        """Refuse a call we will not serve (e.g. at capacity).

        Separate from hangup() because vendors distinguish "no" from "goodbye",
        and because on some vendors this is the only place to do it: Asterisk
        can hand the decision back to the dialplan, whereas a cloud vendor with
        no dialplan must answer and say something itself.
        """
