"""The agent pool: who is free, who is on a call.

This is the whole of the capacity logic, and it is deliberately tiny and
completely pure -- no audio, no network, no engine, no transport. It hands out
personas and takes them back. That is all it does, which is what makes it
testable without a phone call.

WHAT A "SLOT" ACTUALLY IS
-------------------------
Holding a persona IS the slot. There is no separate counter to keep in step
with the roster, because a counter and a list are two things that can disagree.
Capacity is `len(personas)`: three personas means three simultaneous calls, and
the fourth caller is told everyone is busy.

WHAT THIS CLASS DOES *NOT* PROTECT
----------------------------------
It stops two callers being handed the same persona. It does NOT provide the
isolation between calls -- that comes from each call building its own engine
from the persona and throwing it away afterwards. A `PoolPersona` is a frozen
description (a name, a voice, some instructions), so passing the same one to a
later caller carries nothing forward. Hand out something stateful here and the
privacy guarantee is gone, quietly.

THREADING RULE
--------------
`acquire()` and `release()` are called ONLY from the asyncio event loop, in the
per-call task. Never from the AudioSocket I/O threads. `asyncio.Lock` is not
thread-safe; it coordinates tasks on one loop, not threads. Calling these from
a socket thread would corrupt the pool in a way that looks like a random,
unreproducible double-booking.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Sequence

from core.config import PoolPersona


@dataclass(frozen=True)
class PoolStats:
    """A snapshot for logging and health checks. Plain numbers, no live view."""

    capacity: int
    free: int
    busy: int
    busy_names: tuple[str, ...]
    free_names: tuple[str, ...]

    def __str__(self) -> str:
        busy = ", ".join(self.busy_names) if self.busy_names else "none"
        return f"{self.free}/{self.capacity} free (on calls: {busy})"


class AgentPool:
    """A fixed roster of personas, at most one call each."""

    def __init__(self, personas: Sequence[PoolPersona]):
        if not personas:
            raise ValueError(
                "AgentPool needs at least one persona; an empty pool can never "
                "answer a call."
            )
        # Names are the identity used in logs and in the busy bookkeeping below,
        # so duplicates would make a release ambiguous. The config loader already
        # rejects them; re-checking here means the class is correct on its own
        # terms and cannot be broken by a caller that skipped the loader.
        names = [p.name.lower() for p in personas]
        if len(set(names)) != len(names):
            raise ValueError(f"AgentPool: persona names must be unique, got {names}")

        self._all: tuple[PoolPersona, ...] = tuple(personas)

        # Free personas, most-recently-released LAST -- acquire() pops from the
        # end, so a persona that just finished a call is the next one handed out.
        # See decisions.md 029 for why last-freed-first rather than round-robin.
        self._free: list[PoolPersona] = list(personas)

        # Busy personas keyed by name. A dict rather than a list because every
        # question we ask ("is this one already out?") is a membership test, and
        # it is what makes release() idempotent for free.
        self._busy: dict[str, PoolPersona] = {}

        # Guards the two lines that move a persona between _free and _busy.
        #
        # Strictly, on a single event loop those lines cannot be interrupted:
        # there is no `await` between reading and writing, so nothing else can
        # run in between. The lock is here anyway, for the failure this class
        # must never have -- the day someone adds an `await` inside the critical
        # section (a metrics call, a database write), the atomicity would vanish
        # silently and the symptom would be two callers hearing the same agent,
        # rarely, under load. Making the critical section explicit costs nothing
        # and keeps that safe.
        self._lock = asyncio.Lock()

    # -- capacity ----------------------------------------------------------
    @property
    def capacity(self) -> int:
        """Maximum simultaneous calls. Fixed at construction: the roster IS N."""
        return len(self._all)

    @property
    def personas(self) -> tuple[PoolPersona, ...]:
        return self._all

    # -- the two operations that matter -------------------------------------
    async def acquire(self) -> PoolPersona | None:
        """Take a free persona, or None if every agent is on a call.

        Returning None rather than raising or blocking is the design choice:
        "everyone is busy" is a normal, expected outcome that the caller handles
        by playing a message and hanging up. It is not an error, and it must
        never wait -- a caller queued behind a lock would hear silence instead
        of being told.
        """
        async with self._lock:
            if not self._free:
                return None
            persona = self._free.pop()
            self._busy[persona.name] = persona
            return persona

    async def release(self, persona: PoolPersona) -> bool:
        """Return a persona to the pool. Safe to call more than once.

        Returns True if this call actually freed the persona, False if it was
        already free or was never in this pool. Never raises -- this runs in a
        `finally`, often while an exception is already on its way out, and a
        second exception there would replace the real one and lose it.

        Idempotence is not politeness, it is a leak guard. The alternative --
        blindly appending to the free list -- would put a persona in the list
        twice on a double release, and then two concurrent callers really could
        be handed the same agent. Silently doing nothing is the safe direction
        to fail in: capacity may look one lower than it is, but no caller is
        ever double-booked.
        """
        async with self._lock:
            if self._busy.pop(persona.name, None) is None:
                return False
            self._free.append(persona)
            return True

    # -- introspection ------------------------------------------------------
    def stats(self) -> PoolStats:
        """Current occupancy. Cheap and lock-free, for logging and health.

        No lock on purpose: this only reads, and every write above happens on
        the same event loop with no `await` between the two container updates,
        so a reader on that loop can never catch the pool half-updated. Taking
        the lock to write a log line would also mean log output could delay a
        call being answered, which is the wrong priority.
        """
        return PoolStats(
            capacity=self.capacity,
            free=len(self._free),
            busy=len(self._busy),
            busy_names=tuple(p.name for p in self._busy.values()),
            free_names=tuple(p.name for p in self._free),
        )

    def __repr__(self) -> str:
        return f"<AgentPool {self.stats()}>"
