"""A small read-only HTTP surface: health, capacity, live calls, metrics.

Runs in the SAME process and on the SAME event loop as the calls. That is the
whole design constraint, and it decides everything below.

WHY IN-PROCESS
--------------
The state worth exposing -- who is free, who is on a call right now -- lives in
memory in this process and nowhere else. A separate service would have to be
told about it, which means either shared storage (a new dependency and a new
thing that can be stale) or the calls doing extra work to publish it. An HTTP
handler that reads a dictionary costs nothing.

WHAT IT COSTS, AND THE RULE THAT FOLLOWS
----------------------------------------
Sharing the loop means **a slow handler is a dropped call**. This codebase has
lost calls to event-loop stalls twice ([[bugs]] B-001, B-011), and a metrics
scrape every fifteen seconds is a reliable way to find a third.

So: **every handler here reads memory and returns. No database queries, no file
I/O, no awaiting anything that can be slow.** Historical questions belong to a
query against `records/calls.db`, run by whoever is asking, not to this server.
If a handler ever needs real work, it moves off the loop or moves out of the
process -- it does not get "just this one await".

READ-ONLY, AND BOUND TO LOOPBACK
--------------------------------
Nothing here changes the service's behaviour: there is no endpoint to hang up a
call, reload config, or take an agent out of the pool. That is a deliberate
limit, not an oversight -- a control plane that can act needs authentication,
and this has none.

It binds to 127.0.0.1 by default. **`/calls` returns caller phone numbers**, so
exposing this on 0.0.0.0 publishes personal data to anyone who can reach the
port. See [[decisions]] 043.
"""

from __future__ import annotations

import json
from typing import Any

from aiohttp import web
from loguru import logger

from core.live import Counters, LiveCalls
from core.pool import AgentPool
from core.records import RecordWriter


def _json(payload: Any, status: int = 200) -> web.Response:
    """JSON with a trailing newline, so `curl` output is readable in a terminal."""
    return web.Response(
        body=json.dumps(payload, indent=2, default=str) + "\n",
        status=status,
        content_type="application/json",
    )


class ApiServer:
    """Serves the control plane. Started after the transport, stopped before it."""

    def __init__(
        self,
        pool: AgentPool,
        live: LiveCalls,
        counters: Counters,
        records: RecordWriter | None = None,
        host: str = "127.0.0.1",
        port: int = 8091,
        tenant_id: str = "default",
    ):
        self._pool = pool
        self._live = live
        self._counters = counters
        self._records = records
        self._host = host
        self._port = port
        self._tenant_id = tenant_id
        self._runner: web.AppRunner | None = None

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._index),
                web.get("/health", self._health),
                web.get("/pool", self._pool_state),
                web.get("/calls", self._calls),
                web.get("/metrics", self._metrics),
            ]
        )
        self._runner = web.AppRunner(app, access_log=None)  # access log = loop work
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info(f"API on http://{self._host}:{self._port} (read-only)")
        if self._host not in ("127.0.0.1", "localhost", "::1"):
            # Not refused, because there are legitimate reasons to bind wider --
            # but it must never happen by accident and unnoticed.
            logger.warning(
                f"API is bound to {self._host}, NOT loopback. It has no "
                "authentication and /calls exposes caller numbers."
            )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- handlers. Each one reads memory and returns. -----------------------
    async def _index(self, _request: web.Request) -> web.Response:
        return _json(
            {
                "service": "voice-agent",
                "tenant": self._tenant_id,
                "endpoints": ["/health", "/pool", "/calls", "/metrics"],
            }
        )

    async def _health(self, _request: web.Request) -> web.Response:
        """Liveness and capacity in one call.

        `status` is "ok" until the pool is full, then "at_capacity" -- which is
        NOT a failure and returns 200. A load balancer must not pull a node out
        for being busy; being busy is what it is for. It goes unhealthy only if
        something is actually wrong.
        """
        stats = self._pool.stats()
        return _json(
            {
                "status": "ok" if stats.free else "at_capacity",
                "tenant": self._tenant_id,
                "uptime_s": self._counters.uptime_s,
                "capacity": stats.capacity,
                "free": stats.free,
                "busy": stats.busy,
                "calls_in_progress": len(self._live),
            }
        )

    async def _pool_state(self, _request: web.Request) -> web.Response:
        stats = self._pool.stats()
        return _json(
            {
                "capacity": stats.capacity,
                "free": stats.free,
                "busy": stats.busy,
                "free_agents": list(stats.free_names),
                "busy_agents": list(stats.busy_names),
            }
        )

    async def _calls(self, _request: web.Request) -> web.Response:
        """Calls in progress. **Contains caller phone numbers.**

        Only calls happening right now -- a finished call is a database row, not
        an API call, and asking this endpoint for history would mean a query on
        the event loop.
        """
        calls = self._live.snapshot()
        return _json({"count": len(calls), "calls": calls})

    async def _metrics(self, _request: web.Request) -> web.Response:
        """Prometheus text format.

        Counters for things that accumulate, gauges read live for things that
        are true right now. No caller identifiers: a metrics endpoint is the one
        most likely to be scraped into a system with looser access rules than
        this one, so it carries numbers only.
        """
        stats = self._pool.stats()
        c = self._counters
        lines = [
            "# HELP voiceagent_uptime_seconds Seconds since the process started.",
            "# TYPE voiceagent_uptime_seconds gauge",
            f"voiceagent_uptime_seconds {c.uptime_s}",
            "# HELP voiceagent_pool_capacity Maximum simultaneous calls.",
            "# TYPE voiceagent_pool_capacity gauge",
            f"voiceagent_pool_capacity {stats.capacity}",
            "# HELP voiceagent_pool_busy Agents on a call right now.",
            "# TYPE voiceagent_pool_busy gauge",
            f"voiceagent_pool_busy {stats.busy}",
            "# HELP voiceagent_pool_free Agents available right now.",
            "# TYPE voiceagent_pool_free gauge",
            f"voiceagent_pool_free {stats.free}",
            "# HELP voiceagent_calls_total Calls answered since start.",
            "# TYPE voiceagent_calls_total counter",
            f"voiceagent_calls_total {c.calls_total}",
            "# HELP voiceagent_calls_rejected_total Callers refused at capacity.",
            "# TYPE voiceagent_calls_rejected_total counter",
            f"voiceagent_calls_rejected_total {c.calls_rejected_total}",
            "# HELP voiceagent_calls_failed_total Calls whose engine raised.",
            "# TYPE voiceagent_calls_failed_total counter",
            f"voiceagent_calls_failed_total {c.calls_failed_total}",
            "# HELP voiceagent_transfers_total Calls handed to a human.",
            "# TYPE voiceagent_transfers_total counter",
            f"voiceagent_transfers_total {c.transfers_total}",
            "# HELP voiceagent_frames_dropped_total Inbound audio frames discarded.",
            "# TYPE voiceagent_frames_dropped_total counter",
            f"voiceagent_frames_dropped_total {c.frames_dropped_total}",
            "# HELP voiceagent_pacer_slips_total Times outbound audio ran late.",
            "# TYPE voiceagent_pacer_slips_total counter",
            f"voiceagent_pacer_slips_total {c.pacer_slips_total}",
        ]
        if self._records is not None:
            r = self._records.stats
            lines += [
                "# HELP voiceagent_records_written_total Records persisted.",
                "# TYPE voiceagent_records_written_total counter",
                f"voiceagent_records_written_total {r.written}",
                "# HELP voiceagent_records_dropped_total Records lost, queue full.",
                "# TYPE voiceagent_records_dropped_total counter",
                f"voiceagent_records_dropped_total {r.dropped}",
                "# HELP voiceagent_records_failed_total Records lost, store error.",
                "# TYPE voiceagent_records_failed_total counter",
                f"voiceagent_records_failed_total {r.failed}",
            ]
        return web.Response(
            text="\n".join(lines) + "\n",
            content_type="text/plain",
            charset="utf-8",
        )
