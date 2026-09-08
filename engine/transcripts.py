"""
Call transcripts: what was said, written to disk.

Two artifacts per call, in `recordings/`:
  <call_id>-<stamp>-transcript.jsonl    appended LIVE, one line per utterance
  <call_id>-<stamp>-conversation.json   both sides, written once the call is over

The live .jsonl matters: a crashed or dropped call still leaves a usable record,
which the end-of-call JSON alone would not.

TWO THINGS CHANGED IN STAGE B, BOTH OF THEM BUGS RATHER THAN FEATURES
---------------------------------------------------------------------
**The files used to be orphaned.** The filename was a timestamp plus a fresh
random hex tag unrelated to anything, and neither file contained the call id,
the caller, the persona or the Asterisk channel. Given a file you could not tell
which call produced it except by matching wall-clock seconds against the log.
Now both files are keyed by `call_id` and carry a `call` header naming every
identifier, including Asterisk's own `uniqueid`/`linkedid` so a record can be
joined to a CDR.

**The writes used to block the event loop.** `open()` + `write()` ran inline in
the frame handler, on the loop, on every utterance. This codebase has dropped
calls twice from event-loop stalls ([[bugs]] B-001, B-011); doing disk I/O there
was an outage waiting for a slow disk. Records now go on a queue that a writer
task drains, doing the actual file write in a thread.

(Text only -- recording call AUDIO is deliberately out of scope for now.)
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


def _now() -> str:
    """Timestamps are UTC and carry milliseconds.

    The old records were local-time to the second, which is two problems: a
    second is far too coarse to derive turn latency from, and local time makes
    records from two machines impossible to order once anyone deploys a second
    node.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class TranscriptRecorder(FrameProcessor):
    """Writes every finalized caller utterance to a .jsonl file as it happens.

    A FrameProcessor is just a stage in the pipeline: frames come in, you
    inspect the ones you care about, then you MUST pass every frame along with
    push_frame() or the pipeline stalls behind you.
    """

    def __init__(self, path: Path, call: dict | None = None):
        super().__init__()
        self._path = path
        # Written as the first line, so the file says what call it belongs to
        # without needing the log to interpret it.
        self._header = {"type": "call", "at": _now(), **(call or {})}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None
        self._turn = 0

    # -- the write path ----------------------------------------------------
    def _start_writer(self):
        """Started lazily on first use, so nothing runs for a call that never
        produces a transcript, and so no Pipecat lifecycle hook is needed."""
        if self._writer_task is None:
            self._queue.put_nowait(self._header)
            self._writer_task = asyncio.create_task(self._drain())

    def _append(self, record: dict):
        self._start_writer()
        self._queue.put_nowait(record)

    async def _drain(self):
        """Own the file for the life of the call; write off the event loop."""
        try:
            while True:
                record = await self._queue.get()
                if record is None:
                    return
                try:
                    await asyncio.to_thread(self._write_line, record)
                except Exception as e:  # noqa: BLE001
                    # A transcript is evidence, not the product. Losing a line
                    # must never take the call down with it.
                    logger.warning(f"could not write transcript line: {e}")
        except asyncio.CancelledError:
            raise

    def _write_line(self, record: dict):
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    async def close(self):
        """Flush and stop. Called by the engine in its `finally`.

        Waits for the queue to drain rather than cancelling it: the last thing a
        caller said before hanging up is exactly the line worth keeping.
        """
        if self._writer_task is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(self._writer_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._writer_task.cancel()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"transcript writer did not close cleanly: {e}")

    # -- the pipeline stage -------------------------------------------------
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
            self._turn += 1
            self._append(
                {
                    "type": "turn",
                    "seq": self._turn,
                    "at": _now(),
                    "speaker": "caller",
                    "text": frame.text.strip(),
                    "language": str(frame.language) if frame.language else None,
                }
            )
            logger.info(f"CALLER: {frame.text.strip()}")

        # Always forward the frame, whether or not we were interested in it.
        await self.push_frame(frame, direction)


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


def save_conversation(
    context, path: Path, started: datetime, call: dict | None = None
):
    """Write the complete two-sided conversation once the call is over.

    `call` carries the identifiers -- call id, caller, persona, Asterisk channel
    and uniqueid/linkedid, tenant. Without them this file was unattributable:
    you could read what was said and not know who said it, to whom, or which
    Asterisk record it corresponds to.
    """
    try:
        messages = [
            r
            for r in (_message_to_record(m) for m in context.messages)
            if r.get("role") != "system"
        ]
        ended = datetime.now()
        path.write_text(
            json.dumps(
                {
                    "call": call or {},
                    "started": started.astimezone(timezone.utc).isoformat(
                        timespec="milliseconds"
                    ),
                    "ended": ended.astimezone(timezone.utc).isoformat(
                        timespec="milliseconds"
                    ),
                    "duration_s": round((ended - started).total_seconds(), 3),
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
