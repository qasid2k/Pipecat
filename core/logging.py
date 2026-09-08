"""Logging setup: one call, one thread of evidence.

Until now this service logged to stderr with loguru's defaults, unstructured and
unkeyed. That is survivable with one call at a time and useless with three: the
VAD lines, the `CALLER:` transcripts, the DTMF events and the 5-second audio
heartbeats carried nothing to say which call they belonged to, so under
concurrency they interleaved into something no human could untangle and no tool
could parse.

Two sinks, doing two different jobs:

  * **console** -- for a human watching a call happen. Short call id, colour,
    one line per event.
  * **JSON file** (optional) -- for a machine reading it afterwards. Full call
    id, every bound field, one object per line, rotated so it cannot fill the
    disk.

HOW A LOG LINE GETS ITS call_id
-------------------------------
`logger.contextualize(call_id=...)` in `run_call`. It stores the binding in a
contextvar, so **everything** logged inside that call's task inherits it --
including code that has never heard of logging setup, like the engine and the
transcript recorder. Nothing needs to pass a logger around.

The exception is the AudioSocket I/O threads. `threading.Thread` does not copy
the caller's context, so those two threads per call are outside the contextvar
and must bind explicitly. `AudioSocketConnection.log` does that.
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

# Shown when a line is not part of any call: startup, shutdown, the pool's own
# accounting. A visible placeholder rather than a blank, so a missing call_id
# looks deliberate rather than broken.
NO_CALL = "-"

CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss.SSS}</green> "
    "<level>{level: <7}</level> "
    "<cyan>{extra[short]}</cyan> "
    "<level>{message}</level>"
)


def _patcher(record) -> None:
    """Give every record a short call id for the console.

    The console shows 8 characters because a 36-character UUID on every line is
    unreadable; the JSON sink keeps the full value, because that is the one that
    has to join to a database row and an Asterisk CDR. Same identifier, two
    audiences.
    """
    call_id = str(record["extra"].get("call_id") or NO_CALL)
    record["extra"]["short"] = call_id[:8] if call_id != NO_CALL else NO_CALL


def configure_logging(
    level: str = "INFO",
    file: str | Path | None = None,
    rotation: str = "50 MB",
    retention: str = "14 days",
    tenant_id: str = "default",
    base_dir: Path | None = None,
) -> None:
    """Install the sinks. Call once, at startup, before anything else logs."""
    logger.remove()  # drop loguru's default stderr sink, or we log everything twice

    # Defaults for the bound fields, so a format string referencing them can
    # never raise on a line that was logged outside a call.
    logger.configure(
        extra={"call_id": NO_CALL, "short": NO_CALL, "tenant_id": tenant_id},
        patcher=_patcher,
    )

    logger.add(sys.stderr, level=level, format=CONSOLE_FORMAT, enqueue=False)

    if file:
        path = Path(file)
        if not path.is_absolute() and base_dir:
            path = base_dir / path
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            path,
            level=level,
            # serialize=True writes one JSON object per line, including
            # everything bound via contextualize()/bind() -- which is the whole
            # point: call_id and tenant_id land in the record as fields, not
            # buried in a formatted string somebody has to regex back out.
            serialize=True,
            rotation=rotation,
            retention=retention,
            # enqueue=True hands writes to a background thread. Deliberate: this
            # sink writes to disk, and disk writes on the event loop are exactly
            # what dropped calls in B-011. It also makes the sink safe to use
            # from the AudioSocket I/O threads.
            enqueue=True,
            backtrace=False,
            diagnose=False,  # never serialise local variables -- they hold API keys
        )
        logger.info(f"Logging JSON to {path} (rotate {rotation}, keep {retention})")
