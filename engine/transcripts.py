"""
Call transcripts: what was said, written to disk.

Two artifacts per call, in `recordings/`:
  <stamp>-transcript.jsonl    appended LIVE, one line per caller utterance
  <stamp>-conversation.json   both sides, written once the call is over

The live .jsonl matters: a crashed or dropped call still leaves a usable record,
which the end-of-call JSON alone would not.

(Text only -- recording call AUDIO is deliberately out of scope.)
"""

import json
from datetime import datetime
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class TranscriptRecorder(FrameProcessor):
    """Writes every finalized caller utterance to a .jsonl file as it happens.

    A FrameProcessor is just a stage in the pipeline: frames come in, you
    inspect the ones you care about, then you MUST pass every frame along with
    push_frame() or the pipeline stalls behind you.
    """

    def __init__(self, path: Path):
        super().__init__()
        self._path = path

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # VAD events -- these prove whether the caller's audio is actually being
        # heard. If they never fire, no speech is reaching the pipeline (an
        # inbound-audio problem), regardless of what the caller says.
        if isinstance(frame, UserStartedSpeakingFrame):
            logger.info("VAD: caller started speaking")
        elif isinstance(frame, UserStoppedSpeakingFrame):
            logger.info("VAD: caller stopped speaking")

        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            self._append(
                {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "speaker": "caller",
                    "text": frame.text.strip(),
                    "language": str(frame.language) if frame.language else None,
                }
            )
            logger.info(f"CALLER: {frame.text.strip()}")

        # Always forward the frame, whether or not we were interested in it.
        await self.push_frame(frame, direction)

    def _append(self, record: dict):
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _message_to_record(m):
    """Normalize one context message to a plain dict for saving.

    context.messages mixes plain dicts (standard user/assistant turns) with
    LLMSpecificMessage objects (provider-specific -- these appear once a tool
    like transfer_to_department runs). The latter have no .get(), which used to
    crash the save. LLMSpecificMessage wraps the real payload under .message.
    """
    if isinstance(m, dict):
        return m
    inner = getattr(m, "message", m)  # unwrap LLMSpecificMessage
    if isinstance(inner, dict):
        return inner
    # Provider object we can't introspect: stringify so the dump never fails.
    return {"role": getattr(inner, "role", "tool"), "content": str(inner)}


def save_conversation(context, path: Path, started: datetime):
    """Write the complete two-sided conversation once the call is over."""
    try:
        messages = [
            r
            for r in (_message_to_record(m) for m in context.messages)
            if r.get("role") != "system"
        ]
        path.write_text(
            json.dumps(
                {
                    "started": started.isoformat(timespec="seconds"),
                    "ended": datetime.now().isoformat(timespec="seconds"),
                    "turns": len(messages),
                    "conversation": messages,
                },
                indent=2,
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
        logger.info(f"Conversation saved -> {path.name} ({len(messages)} turns)")
    except Exception as e:
        logger.error(f"Could not save conversation: {e}")
