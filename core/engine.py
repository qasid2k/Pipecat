"""
Phase 3: the conversation-engine contract.

The other half of the seam. `core/transport.py` says how a call reaches us;
this says what runs on it. Between them, `bot.py` is reduced to plumbing:

    async for session in transport.listen():     # a vendor produced a call
        await engine.run(session)                # an engine talks on it

WHY WRAP PIPECAT AT ALL
-----------------------
Pipecat is an in-process BSD-2 library, not a service, and it ALREADY abstracts
STT/LLM/TTS providers -- so wrapping each provider would be pure duplication.
The risk actually worth insuring against is different: that Pipecat itself turns
out to be the wrong choice. This one interface covers exactly that. Replacing it
means writing one new Engine and changing nothing else.

Note what this contract does NOT mention: pipelines, frames, processors,
aggregators, VAD. Those are Pipecat's vocabulary, and they stop here.
"""

from abc import ABC, abstractmethod

from core.transport import CallSession


class Engine(ABC):
    """Runs the conversation for one call.

    One Engine instance per call -- it may hold per-call state (conversation
    history, transcripts), so instances are not reusable across calls.
    """

    @abstractmethod
    async def run(self, session: CallSession) -> None:
        """Talk to the caller until the call ends, then return.

        Returns normally when the conversation is over for ANY reason: the
        caller hung up, the call was transferred away, an idle timeout expired,
        or the engine finished what it had to say.

        Must not raise for an ordinary call ending, and must clean up its own
        resources before returning. It must NOT hang up the session -- the
        caller of run() owns that, in a `finally`, so cleanup is guaranteed on
        every path including an exception.
        """
