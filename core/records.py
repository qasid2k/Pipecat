"""Call records: what happened, in a form you can query.

Logs answer "what happened on this call". Records answer "how many calls did
Daniel take last week, how many were transferred to billing, and what was the
average handle time" -- questions no amount of grepping a log file answers well.

WHAT IS HERE AND WHAT IS NOT
----------------------------
This module is shapes and contracts only: the two records, the `CallStore`
interface, and the writer that gets records to a store without blocking the
event loop. It knows no SQL and imports no database driver. Implementations live
outside `core/` (see `stores/`) and are built by `factories.py`, for the same
reason the transports and engines are: a contract that imports its own
implementations is not a contract.

THE RULE THAT SHAPES ALL OF THIS
--------------------------------
**A record is evidence, not the product.** Losing one is bad. Dropping a call
because a database was slow, full, or restarting is far worse -- the caller is
real and the row is not. So every path here fails soft: the writer never blocks
the call, never raises into it, and drops records rather than applying
back-pressure to a live conversation.
"""

from __future__ import annotations

import asyncio
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

# Identifies which process wrote a row. One value today; it exists because Stage
# F puts several workers behind one Asterisk, and "which node took this call"
# becomes the first question asked when one of them misbehaves.
NODE_ID = f"{socket.gethostname()}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CallRecord:
    """One row per call. Every field has a source; nothing here is derived.

    The identifier trio is the point of the whole record:
      * `call_id`      -- ours (the AudioSocket UUID). Joins to the logs and to
                          the transcript files.
      * `uniqueid`     -- Asterisk's. Joins to CDR and CEL.
      * `linkedid`     -- Asterisk's, constant across a transfer. Joins the two
                          halves of a transferred call back together.
    """

    call_id: str
    tenant_id: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_s: float = 0.0

    caller_id: str = "unknown"
    uniqueid: str = ""
    linkedid: str = ""

    persona: str = ""
    voice: str = ""
    llm_model: str = ""

    # How the call finished. `end_reason` is the transport's ("caller hung up");
    # `cause` is the engine's ("the pipeline finished on its own"). They answer
    # different questions and disagreeing is informative, so both are kept.
    end_reason: str = ""
    cause: str = ""
    # The department, if the LLM transferred. NULL means it handled the call
    # itself -- which makes "what fraction did we deflect" a single query.
    transferred_to: str | None = None

    # Audio counters. `frames_dropped` and `pacer_slips` are the overload
    # signals: any non-zero value means this caller was served worse than they
    # should have been, and they are what a capacity ceiling is measured against.
    frames_in: int = 0
    frames_out: int = 0
    frames_out_real: int = 0
    frames_dropped: int = 0
    pacer_slips: int = 0

    transcript_path: str = ""
    conversation_path: str = ""
    node_id: str = NODE_ID


@dataclass(frozen=True)
class TurnRecord:
    """One row per utterance. `seq` orders them within a call.

    Kept separate from CallRecord rather than nested, because the questions are
    different: calls are counted and averaged, turns are read and scored.
    """

    call_id: str
    seq: int
    at: datetime
    speaker: str  # "caller" | "agent"
    text: str
    language: str | None = None


class CallStore(ABC):
    """Somewhere records go. Implementations live outside core/."""

    @abstractmethod
    async def start(self) -> None:
        """Open the connection and make sure the schema exists."""

    @abstractmethod
    async def save_call(self, record: CallRecord) -> None:
        """Insert or replace one call row."""

    @abstractmethod
    async def save_turns(self, turns: Sequence[TurnRecord]) -> None:
        """Insert a batch of turn rows."""

    @abstractmethod
    async def close(self) -> None:
        """Flush and release. Idempotent."""

    @property
    def describe(self) -> str:
        """One line for the startup banner, e.g. 'sqlite -> records.db'."""
        return type(self).__name__


class NullCallStore(CallStore):
    """Records disabled. Accepts everything, keeps nothing.

    A real implementation rather than an `if store is not None` at every call
    site: the caller should not have to know whether recording is switched on,
    and a branch repeated in five places is a branch that will be forgotten in
    one of them.
    """

    async def start(self) -> None:
        pass

    async def save_call(self, record: CallRecord) -> None:
        pass

    async def save_turns(self, turns: Sequence[TurnRecord]) -> None:
        pass

    async def close(self) -> None:
        pass

    @property
    def describe(self) -> str:
        return "disabled"


@dataclass
class WriterStats:
    written: int = 0
    dropped: int = 0
    failed: int = 0

    def __str__(self) -> str:
        s = f"{self.written} written"
        if self.dropped:
            s += f", {self.dropped} DROPPED (queue full)"
        if self.failed:
            s += f", {self.failed} FAILED (store error)"
        return s


class RecordWriter:
    """Gets records to a store without ever making a call wait.

    `submit()` is synchronous, non-blocking and never raises: it puts the record
    on a bounded queue and returns. A single background task drains the queue and
    talks to the store. That indirection is the whole design, and it exists for
    three reasons:

      * **The store does I/O.** A database write on the event loop is the same
        hazard that dropped calls twice in this codebase ([[bugs]] B-001, B-011).
      * **The store can be slow or down.** Awaiting it from `run_call` would make
        a database problem into a telephony problem.
      * **The queue is bounded.** If records are produced faster than the store
        accepts them, the right failure is to lose records and say so loudly --
        not to grow a queue until the process dies, and not to slow down calls.

    Failing soft is deliberate everywhere here. A record is evidence; the caller
    is real.
    """

    def __init__(self, store: CallStore, max_queue: int = 1000):
        self._store = store
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task | None = None
        self._logger = None  # injected by set_logger, so core/ stays log-agnostic
        self.stats = WriterStats()

    def set_logger(self, logger) -> None:
        self._logger = logger

    def _log(self, level: str, message: str) -> None:
        if self._logger is not None:
            getattr(self._logger, level)(message)

    async def start(self) -> None:
        await self._store.start()
        self._task = asyncio.create_task(self._drain())

    def submit(self, item: CallRecord | Sequence[TurnRecord]) -> None:
        """Hand over a record. Returns immediately, always."""
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self.stats.dropped += 1
            # WARNING, not DEBUG: this means the store cannot keep up, and the
            # analytics built on these rows are now quietly incomplete.
            self._log(
                "warning",
                f"record queue full -- dropping a record "
                f"({self.stats.dropped} dropped so far)",
            )

    async def _drain(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            try:
                if isinstance(item, CallRecord):
                    await self._store.save_call(item)
                else:
                    await self._store.save_turns(item)
                self.stats.written += 1
            except Exception as e:  # noqa: BLE001
                self.stats.failed += 1
                # Swallowed on purpose. The alternative -- letting this task die
                # on the first bad row -- would silently stop recording for the
                # life of the process, which is worse than losing one row.
                self._log("warning", f"could not write record: {e}")

    async def close(self, timeout: float = 5.0) -> None:
        """Drain what is queued, then release the store.

        Waits rather than cancelling: at shutdown the queue holds the records of
        the calls that just ended, which are exactly the ones worth keeping.
        Time-boxed, because a hung store must not hold up a shutdown.
        """
        if self._task is not None:
            await self._queue.put(None)
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._log("warning", "record writer did not drain in time")
                self._task.cancel()
        await self._store.close()
