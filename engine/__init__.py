"""Conversation engines. Everything that imports Pipecat lives in here.

  pipecat_engine    -- PipecatEngine: Deepgram STT -> Gemini -> Deepgram TTS
  session_transport -- bridges our CallSession to Pipecat's transport classes
  transcripts       -- per-call transcript recording

Swapping Pipecat out means adding one module here that implements
`core.engine.Engine`, and changing nothing anywhere else.
"""

from engine.pipecat_engine import PipecatEngine

__all__ = ["PipecatEngine"]
