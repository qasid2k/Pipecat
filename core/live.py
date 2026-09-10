"""What is happening right now, and what has happened since boot.

Two small pieces of shared state that nothing else owns:

  * `LiveCalls`  -- who is on a call at this instant, with enough detail to be
    useful on a supervisor's screen: which agent, which caller, how long.
  * `Counters`   -- totals since the process started, for `/metrics`.

WHY NOT JUST READ THE POOL, OR THE DATABASE
-------------------------------------------
`PoolStats` answers "how many agents are free" and nothing else -- it knows
Sarah is busy but not who she is talking to, or for how long. The database
answers questions about calls that have *finished*; a row is written in the
`finally`, so a call in progress does not appear in it at all. "Who is on a call
right now" is a question only this can answer.

WHY THIS IS NOT THE POOL'S JOB
------------------------------
The pool decides who may answer; this observes what happened. Merging them would
put display concerns inside the one piece of code where a mistake double-books a
caller. `core/pool.py` stays as small as it is for a reason.

FAILING SOFT
------------
Same rule as the record path: this is observation, and observation must never be
able to break a call. Every method here is total -- no exceptions, no blocking,
no awaits. A supervisor screen going stale is not worth a dropped call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Lock


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LiveCall:
    """One call in progress."""

    call_id: str
    caller_id: str
    persona: str
    voice: str
    started_at: datetime

    def as_dict(self, now: datetime | None = None) -> dict:
        now = now or _utcnow()
        return {
            "call_id": self.call_id,
            "caller_id": self.caller_id,
            "persona": self.persona,
            "voice": self.voice,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            # Computed on read, not stored: a duration is only meaningful at the
            # moment it is asked for.
            "duration_s": round((now - self.started_at).total_seconds(), 1),
        }


class LiveCalls:
    """The calls in progress, keyed by call id.

    Guarded by a plain `threading.Lock` rather than an `asyncio.Lock`, and
    deliberately: the API handlers read this from the event loop, but nothing
    stops a future caller reading it from a thread, and the critical sections
    are three dictionary operations. A blocking lock held for nanoseconds is
    safe on the loop; an asyncio lock would make every read a coroutine and
    make this unusable from the I/O threads that already exist.
    """

    def __init__(self):
        self._calls: dict[str, LiveCall] = {}
        self._lock = Lock()

    def started(self, call: LiveCall) -> None:
        with self._lock:
            self._calls[call.call_id] = call

    def ended(self, call_id: str) -> None:
        """Remove a call. Safe if it was never added or is already gone."""
        with self._lock:
            self._calls.pop(call_id, None)

    def snapshot(self) -> list[dict]:
        """Every call in progress, longest-running first.

        Longest first because that is the order a supervisor cares about: the
        call that has been going twenty minutes is the one worth looking at.
        """
        now = _utcnow()
        with self._lock:
            calls = list(self._calls.values())
        return [c.as_dict(now) for c in sorted(calls, key=lambda c: c.started_at)]

    def __len__(self) -> int:
        with self._lock:
            return len(self._calls)


@dataclass
class Counters:
    """Totals since the process started.

    Monotonic counters only, plus the boot time. Deliberately NOT gauges: a
    counter that only ever increases can be scraped at any interval and still
    give a correct rate, whereas a gauge sampled every 30 s can miss a spike
    entirely. Current values (free agents, calls in progress) are read live from
    the pool and `LiveCalls` at scrape time instead.
    """

    started_at: datetime = field(default_factory=_utcnow)
    calls_total: int = 0
    calls_rejected_total: int = 0
    calls_failed_total: int = 0
    transfers_total: int = 0
    # Audio degradation, summed across every call that has ended. The two
    # numbers a capacity ceiling is measured against -- see [[decisions]] 038.
    frames_dropped_total: int = 0
    pacer_slips_total: int = 0

    @property
    def uptime_s(self) -> float:
        return round((_utcnow() - self.started_at).total_seconds(), 1)

    def as_dict(self) -> dict:
        return {
            "uptime_s": self.uptime_s,
            "calls_total": self.calls_total,
            "calls_rejected_total": self.calls_rejected_total,
            "calls_failed_total": self.calls_failed_total,
            "transfers_total": self.transfers_total,
            "frames_dropped_total": self.frames_dropped_total,
            "pacer_slips_total": self.pacer_slips_total,
        }
