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

import asyncio
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

from aiohttp import WSCloseCode, web
from loguru import logger

from core.live import Counters, LiveCalls
from core.pool import AgentPool
from core.records import CallFilter, RecordWriter


# /history page size. Capped so a hand-typed ?limit= cannot make one request
# read the whole table into memory on the call process.
HISTORY_DEFAULT = 50
HISTORY_MAX = 200
# Call ids are UUIDs (or `direct-<addr>` for 6000 calls). Anything else in the
# URL is a typo or a probe, and gets a 400 before it reaches the database.
CALL_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
# Where engine/pipecat_engine.py writes transcripts. The detail view reads ONLY
# inside it: the path comes from a database row, and a row is not a promise.
RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "recordings"
# The supervisor web app: a React build, committed to git so the VM never needs
# Node ([[decisions]] 056). Source lives in web/; `npm run build` writes here.
STATIC_DIR = Path(__file__).resolve().parent / "static"
# Vite names every asset after a hash of its content (index-DhyzqFko.js), so a
# changed file gets a new name: safe to let browsers cache these for a year.
ASSET_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
ASSET_CACHE = "public, max-age=31536000, immutable"


def normalize_conversation(messages: list) -> list[dict]:
    """conversation.json's LLM messages -> `[{"speaker", "text"}]` for a person.

    `user` is the caller; `assistant` (or Google's `model`) is the agent. Tool
    calls and their results are machinery, not conversation, so they are
    dropped. Content comes as a string, a list of typed parts, or Google's
    `parts`; all three reduce to their text.
    """
    out = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        speaker = {"user": "caller", "assistant": "agent", "model": "agent"}.get(m.get("role"))
        if speaker is None:
            continue
        content = m.get("content", m.get("parts"))
        if isinstance(content, list):
            text = " ".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("text")
            )
        else:
            text = str(content or "")
        if text.strip():
            out.append({"speaker": speaker, "text": text.strip()})
    return out


def _read_conversation(path: str, root: Path) -> list[dict] | None:
    """Read one call's conversation.json, or None. Runs in a thread.

    Refuses anything that does not resolve to a file INSIDE `root`, so a bad
    or tampered row cannot turn this endpoint into "read any file on the VM".
    """
    if not path:
        return None
    try:
        target = Path(path).resolve()
        if not target.is_relative_to(root.resolve()) or not target.is_file():
            return None
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return normalize_conversation(data.get("conversation", [])) if isinstance(data, dict) else None


def _parse_filters(q) -> CallFilter | None:
    """Query string -> CallFilter. Raises ValueError with a message for a 400."""
    for key in ("since", "until"):
        if q.get(key):
            try:
                date.fromisoformat(q[key])
            except ValueError:
                raise ValueError(f"{key} must be a date like 2026-09-28") from None
    outcome = q.get("outcome") or None
    if outcome not in (None, "transferred", "handled"):
        raise ValueError("outcome must be 'transferred' or 'handled'")
    f = CallFilter(
        since=q.get("since") or None,
        until=q.get("until") or None,
        persona=(q.get("persona") or "").strip()[:64] or None,
        transferred={"transferred": True, "handled": False}.get(outcome),
        caller=(q.get("caller") or "").strip()[:32] or None,
    )
    return None if f == CallFilter() else f


def _rss_bytes() -> int | None:
    """This process's resident memory, from procfs. None where there is none.

    A file read inside a handler, which the rule at the top of this module
    forbids -- deliberately excepted: /proc is generated by the kernel on read
    and never touches a disk, so it costs microseconds, not the milliseconds of
    a real file. Linux only; the VM is Linux, and the dev laptop simply omits the
    metric rather than reporting a wrong number.
    """
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024  # reported in kB
    except (OSError, ValueError, IndexError):
        return None
    return None


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
        engine_provider: str = "unknown",
        recordings_dir: Path = RECORDINGS_DIR,
    ):
        self._engine_provider = engine_provider
        self._recordings_dir = recordings_dir
        self._pool = pool
        self._live = live
        self._counters = counters
        self._records = records
        self._host = host
        self._port = port
        self._tenant_id = tenant_id
        self._runner: web.AppRunner | None = None
        self._dashboard: str | None = None
        # Connected dashboards. One shared broadcast task serves all of them, so
        # the cost of watching does not scale with the number of watchers.
        self._sockets: set[web.WebSocketResponse] = set()
        self._broadcast: asyncio.Task | None = None
        # Signature of the state every connected dashboard has already been
        # sent. Shared, not per-socket: a new connection is given the current
        # state directly, so after that everyone is at the same point.
        self._last_sig: str | None = None

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        # Read the page once, into memory. Serving it from disk on every
        # request would be file I/O on the call loop, for a file that never
        # changes while the process is running. (The JS/CSS assets are served
        # by aiohttp's FileResponse, which does its file work off the loop.)
        try:
            self._dashboard = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        except OSError as e:
            self._dashboard = None
            logger.warning(
                f"dashboard page unavailable ({e}). The web app is built with "
                "`npm run build` in web/, and api/static/ is committed -- is it "
                "missing from this checkout?"
            )

        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._dashboard_page),
                web.get("/assets/{name}", self._asset),
                web.get("/api", self._index),
                web.get("/health", self._health),
                web.get("/pool", self._pool_state),
                web.get("/calls", self._calls),
                web.get("/history", self._history),
                web.get("/history/{call_id}", self._call_detail),
                web.get("/metrics", self._metrics),
                web.get("/live", self._live_socket),
            ]
        )
        self._runner = web.AppRunner(app, access_log=None)  # access log = loop work
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        self._broadcast = asyncio.create_task(self._broadcast_loop())
        logger.info(f"API on http://{self._host}:{self._port} (read-only)")
        if self._host not in ("127.0.0.1", "localhost", "::1"):
            # Not refused, because there are legitimate reasons to bind wider --
            # but it must never happen by accident and unnoticed.
            logger.warning(
                f"API is bound to {self._host}, NOT loopback. It has no "
                "authentication and /calls exposes caller numbers."
            )

    async def stop(self) -> None:
        if self._broadcast is not None:
            self._broadcast.cancel()
            self._broadcast = None
        for ws in list(self._sockets):
            # Close them explicitly so a watching dashboard is told the service
            # is going away, rather than being left to time out.
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"shutting down")
        self._sockets.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- live push ---------------------------------------------------------
    def _state(self) -> dict:
        stats = self._pool.stats()
        return {
            "tenant": self._tenant_id,
            "pool": {
                "capacity": stats.capacity,
                "free": stats.free,
                "busy": stats.busy,
                "free_agents": list(stats.free_names),
                "busy_agents": list(stats.busy_names),
            },
            "calls": self._live.snapshot(),
            "counters": self._counters.as_dict(),
        }

    @staticmethod
    def _signature(state: dict) -> str:
        """What counts as a CHANGE worth sending.

        Deliberately excludes everything that ticks on its own -- uptime, and
        each call's duration. Including them would make every state differ from
        the last, so the socket would push a full update every second forever
        even with nothing happening. The browser derives durations from
        `started_at` on its own clock, which is both cheaper and smoother.
        """
        return json.dumps(
            {
                "pool": state["pool"],
                "calls": [
                    (c["call_id"], c["persona"], c["started_at"])
                    for c in state["calls"]
                ],
                "counters": {
                    k: v for k, v in state["counters"].items() if k != "uptime_s"
                },
            },
            sort_keys=True,
        )

    async def _broadcast_loop(self, tick: float = 1.0) -> None:
        """Push state to every connected dashboard, but only when it changed.

        One task for all clients: the snapshot is computed once per tick no
        matter how many people are watching. An idle service sends nothing at
        all, so a dashboard left open overnight costs one comparison a second.
        """
        while True:
            try:
                await asyncio.sleep(tick)
                if not self._sockets:
                    continue
                state = self._state()
                signature = self._signature(state)
                # `_last_sig` is also set when a socket connects and is handed
                # the state directly. Without that, a fresh connection would be
                # sent the very same state again on the next tick.
                if signature == self._last_sig:
                    continue
                self._last_sig = signature
                await self._push(state)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                # A broadcast failure must never take down the process, and must
                # not stop later broadcasts either.
                logger.warning(f"dashboard broadcast failed: {e}")

    async def _push(self, state: dict) -> None:
        text = json.dumps(state, default=str)
        for ws in list(self._sockets):
            try:
                await ws.send_str(text)
            except Exception:  # noqa: BLE001
                self._sockets.discard(ws)

    async def _live_socket(self, request: web.Request) -> web.WebSocketResponse:
        """Push updates to a dashboard. Read-only: anything sent is ignored."""
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._sockets.add(ws)
        try:
            state = self._state()
            await ws.send_str(json.dumps(state, default=str))
            # This client is now up to date, so the broadcast loop must not
            # immediately resend the same thing on its next tick.
            self._last_sig = self._signature(state)
            # Drain incoming frames so aiohttp can process pings and closes. The
            # payloads are discarded: this socket is an output, and accepting
            # commands would make it a control channel that has no auth.
            async for _ in ws:
                pass
        finally:
            self._sockets.discard(ws)
        return ws

    async def _dashboard_page(self, _request: web.Request) -> web.Response:
        if self._dashboard is None:
            return _json({"error": "dashboard page not available"}, status=404)
        # no-cache on the page itself: it is what points at the current asset
        # names, so a browser must always ask for the latest one.
        return web.Response(
            text=self._dashboard, content_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )

    async def _asset(self, request: web.Request) -> web.StreamResponse:
        """One built JS/CSS file. Plain names only -- no paths, so a request
        can never climb out of api/static/assets."""
        name = request.match_info["name"]
        path = STATIC_DIR / "assets" / name
        if not ASSET_NAME.match(name) or name.startswith(".") or not path.is_file():
            return _json({"error": "no such asset"}, status=404)
        return web.FileResponse(path, headers={"Cache-Control": ASSET_CACHE})

    # -- handlers. Each one reads memory and returns. -----------------------
    async def _index(self, _request: web.Request) -> web.Response:
        return _json(
            {
                "service": "voice-agent",
                "tenant": self._tenant_id,
                "endpoints": [
                    "/", "/health", "/pool", "/calls", "/history",
                    "/history/{call_id}", "/metrics",
                    "/live (ws)",
                ],
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
                # Which engine is answering. Exposed so a load test can refuse to
                # run against the REAL one by accident -- that would open a
                # provider stream per virtual caller and cost money. Also the
                # fastest way to catch a service left in silent mode, where every
                # caller hears nothing.
                "engine": self._engine_provider,
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

    async def _history(self, request: web.Request) -> web.Response:
        """Recently FINISHED calls, newest first. **Contains caller phone numbers**,
        exactly like /calls, and is loopback-only for the same reason (login is
        slice 3 of [[backlog]] IMP-004).

        The one handler that touches the database, so the rule every other
        handler keeps by reading memory is kept here by the store: the query
        runs in a thread on its own read-only connection, and the loop only
        awaits it ([[decisions]] 043, 054).
        """
        try:
            limit = int(request.query.get("limit", HISTORY_DEFAULT))
        except ValueError:
            return _json({"error": "limit must be a whole number"}, status=400)
        limit = max(1, min(limit, HISTORY_MAX))
        try:
            filters = _parse_filters(request.query)
        except ValueError as e:
            return _json({"error": str(e)}, status=400)
        if self._records is None:
            return _json({"count": 0, "calls": []})
        try:
            if filters is None:
                calls = await self._records.recent_calls(limit=limit, tenant_id=self._tenant_id)
            else:
                calls = await self._records.search_calls(
                    limit=limit, tenant_id=self._tenant_id, filters=filters
                )
        except Exception as e:  # noqa: BLE001 -- a store fault must not become a 500 page
            logger.warning(f"/history: could not read call records: {e!r}")
            return _json({"error": "call history is unavailable right now"}, status=503)
        return _json({"count": len(calls), "calls": calls})

    async def _call_detail(self, request: web.Request) -> web.Response:
        """One finished call with its conversation. **Contains what the caller
        said**, so it is loopback-only like the rest (login: IMP-004 slice 3).

        The transcript prefers the call's conversation.json, the only place the
        AGENT's side is kept, read in a thread and only from inside the
        recordings directory. If that file is gone it falls back to the
        caller-only rows in `turns`, and says which one it used.
        """
        call_id = request.match_info["call_id"]
        if not CALL_ID.match(call_id):
            return _json({"error": "not a call id"}, status=400)
        if self._records is None:
            return _json({"error": "call records are disabled"}, status=404)
        try:
            detail = await self._records.call_detail(call_id=call_id, tenant_id=self._tenant_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"/history/{call_id}: could not read call records: {e!r}")
            return _json({"error": "call history is unavailable right now"}, status=503)
        if detail is None:
            return _json({"error": "no such call"}, status=404)

        call = dict(detail["call"])
        path = call.pop("conversation_path", "") or ""
        transcript = await asyncio.to_thread(_read_conversation, path, self._recordings_dir)
        source = "conversation"
        if not transcript:
            transcript = [
                {"speaker": t["speaker"], "text": t["text"]} for t in detail.get("turns", [])
            ]
            source = "turns" if transcript else "none"
        return _json({"call": call, "transcript": transcript, "transcript_source": source})

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
            "# HELP voiceagent_time_to_greeting_seconds Connect to first agent audio.",
            "# TYPE voiceagent_time_to_greeting_seconds summary",
            f"voiceagent_time_to_greeting_seconds_sum {c.greeting_seconds_total:.3f}",
            f"voiceagent_time_to_greeting_seconds_count {c.greetings_total}",
        ]
        # What a call COSTS, in the standard Prometheus process-metric names so
        # any scraper recognises them. CPU is a counter: sample it twice and the
        # difference over the interval is utilisation, which is exactly what the
        # load harness does per level. It includes every thread -- the two I/O
        # threads per call are most of what is being measured.
        cpu = os.times()
        lines += [
            "# HELP process_cpu_seconds_total User and system CPU time spent.",
            "# TYPE process_cpu_seconds_total counter",
            f"process_cpu_seconds_total {cpu.user + cpu.system:.3f}",
        ]
        rss = _rss_bytes()
        if rss is not None:
            lines += [
                "# HELP process_resident_memory_bytes Resident memory size.",
                "# TYPE process_resident_memory_bytes gauge",
                f"process_resident_memory_bytes {rss}",
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
