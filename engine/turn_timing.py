"""How long the agent takes to reply, turn by turn (IMP-016).

The measurement itself is Pipecat's UserBotLatencyObserver: from the moment
the caller ACTUALLY stopped speaking (the voice detector's own confirmation
delay is subtracted) to the agent's first audio. This module only keeps a
call's running summary and turns the observer's breakdown into one readable
log line:

    turn: reply after 1950 ms (end of turn 940 ms · AI first words 720 ms · voice 210 ms)

  end of turn     silence detection + speech-to-text finishing + our
                  end-of-speech wait (turn_taking.silence_timeout_s)
  AI first words  the language model's time to its first output
  voice           text-to-speech's time to its first audio

The parts need not add up to the total: they overlap (TTS starts on the first
words, not the whole reply) and small hand-offs between them aren't counted.

Pure Python -- the Pipecat wiring is in pipecat_engine.attach_reply_timing.
"""

from __future__ import annotations

from statistics import median


class ReplyStats:
    """One call's reply times, in seconds."""

    def __init__(self):
        self._values: list[float] = []

    def add(self, seconds: float) -> None:
        self._values.append(seconds)

    @property
    def count(self) -> int:
        return len(self._values)

    @property
    def total(self) -> float:
        return sum(self._values)

    @property
    def median(self) -> float | None:
        return median(self._values) if self._values else None

    @property
    def slowest(self) -> float | None:
        return max(self._values) if self._values else None


# Which service a TTFB measurement came from, by its Pipecat processor name
# (e.g. "GoogleLLMService#0"). Match the whole "...Service" suffix: a bare
# "TTS" would also match "DeepgramSTTService" (S-TT-Service), filing
# speech-to-text's timing under the voice. First match per label wins.
_SERVICE_PARTS = (("LLMService", "AI first words"), ("TTSService", "voice"))


def turn_parts(breakdown) -> dict[str, int]:
    """A Pipecat LatencyBreakdown -> {"end of turn": ms, "AI first words": ms, "voice": ms},
    leaving out whatever the breakdown doesn't have."""
    parts: dict[str, int] = {}
    if getattr(breakdown, "user_turn_secs", None) is not None:
        parts["end of turn"] = round(breakdown.user_turn_secs * 1000)
    for metric in getattr(breakdown, "ttfb", []) or []:
        for marker, label in _SERVICE_PARTS:
            if marker in metric.processor and label not in parts:
                parts[label] = round(metric.duration_secs * 1000)
    return parts


def format_turn(reply_secs: float, parts: dict[str, int]) -> str:
    line = f"turn: reply after {round(reply_secs * 1000)} ms"
    if parts:
        line += " (" + " · ".join(f"{k} {v} ms" for k, v in parts.items()) + ")"
    return line
