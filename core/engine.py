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
from dataclasses import dataclass

from core.transport import CallSession


@dataclass(frozen=True)
class EngineResult:
    """What the engine knows about a call that nobody else does.

    `run_call` writes the call record, but three of its fields are only visible
    from inside the conversation: why it ended in the engine's terms, whether the
    model chose to hand the caller to a human, and where the transcripts were
    written. Returning them beats the alternatives -- having the engine write the
    record itself (it would need to know about stores, and `run_call` owns the
    call's lifecycle) or passing in a mutable object for it to fill (invisible
    coupling, and no way to tell "not set" from "set to nothing").

    Still no Pipecat vocabulary here. These are facts about a conversation, not
    about a pipeline.
    """

    # The engine's account of the ending. Deliberately distinct from
    # CallSession.end_reason, which is the transport's: "caller hung up" and
    # "the pipeline finished on its own" answer different questions, and the two
    # disagreeing is informative rather than contradictory.
    cause: str = ""
    # The department the model transferred to, or None if it handled the call
    # itself. None rather than "" so `transferred_to IS NULL` is an honest query.
    transferred_to: str | None = None
    transcript_path: str = ""
    conversation_path: str = ""
    turns: int = 0


class Engine(ABC):
    """Runs the conversation for one call.

    One Engine instance per call -- it may hold per-call state (conversation
    history, transcripts), so instances are not reusable across calls.
    """

    @abstractmethod
    async def run(self, session: CallSession) -> EngineResult | None:
        """Talk to the caller until the call ends, then return.

        Returns normally when the conversation is over for ANY reason: the
        caller hung up, the call was transferred away, an idle timeout expired,
        or the engine finished what it had to say.

        Returns an `EngineResult` describing the call, or None -- an engine that
        has nothing to report is allowed, and the record is simply written
        without those fields rather than not written at all.

        Must not raise for an ordinary call ending, and must clean up its own
        resources before returning. It must NOT hang up the session -- the
        caller of run() owns that, in a `finally`, so cleanup is guaranteed on
        every path including an exception.
        """
