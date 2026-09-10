"""Conversation engines.

  pipecat_engine    -- PipecatEngine: Deepgram STT -> Gemini -> Deepgram TTS
  silent_engine     -- SilentEngine: answers, says nothing, for load testing
  session_transport -- bridges our CallSession to Pipecat's transport classes
  transcripts       -- per-call transcript recording

Swapping Pipecat out means adding one module here that implements
`core.engine.Engine`, and changing nothing anywhere else.

NOTHING IS IMPORTED HERE ON PURPOSE
-----------------------------------
This file used to do `from engine.pipecat_engine import PipecatEngine`, which
meant that importing *anything* from this package -- including the engine that
exists specifically to avoid Pipecat -- dragged in onnxruntime, google-genai and
the Deepgram SDK. That is tens of seconds of cold import ([[bugs]] B-011) paid
by code that wanted none of it, and it silently defeated the lazy imports in
`factories.py`.

Import the module you actually want:

    from engine.pipecat_engine import PipecatEngine
    from engine.silent_engine import SilentEngine
"""
