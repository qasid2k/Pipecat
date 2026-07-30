"""Vendor- and engine-neutral core. Nothing in here imports Asterisk or Pipecat.

Anything that knows about a specific vendor belongs in `transports/`; anything
that knows about Pipecat belongs in `engine/`.
"""

from core.engine import Engine
from core.transport import (
    CANONICAL_CHANNELS,
    CANONICAL_FRAME_BYTES,
    CANONICAL_FRAME_MS,
    CANONICAL_SAMPLE_RATE,
    CANONICAL_SAMPLE_WIDTH_BYTES,
    BaseTransport,
    CallSession,
)

__all__ = [
    "Engine",
    "BaseTransport",
    "CallSession",
    "CANONICAL_SAMPLE_RATE",
    "CANONICAL_CHANNELS",
    "CANONICAL_SAMPLE_WIDTH_BYTES",
    "CANONICAL_FRAME_MS",
    "CANONICAL_FRAME_BYTES",
]
