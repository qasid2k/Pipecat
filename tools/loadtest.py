"""Find this machine's call ceiling by pretending to be Asterisk.

Each virtual caller opens an AudioSocket connection, announces a UUID, then
sends a 320-byte frame every 20 ms and drains whatever comes back -- exactly
what Asterisk does on extension 6000. The bot cannot tell the difference.

    # in one terminal, with engine.provider: null in config.yaml
    python bot.py

    # in another
    python tools/loadtest.py --ramp 30 --step 3 --every 5
    python tools/loadtest.py --spike 20 --duration 30

WHY BOTH RAMP AND SPIKE
-----------------------
They find different limits, and this service's known weak point is the second
one. A gradual ramp finds the *sustained* ceiling: threads, CPU, sockets. A
spike finds the *burst* ceiling, which here is dominated by per-call setup --
building a VAD analyzer still stalls the loop for a few hundred milliseconds
when several calls land together ([[decisions]] 047). A ramp-only test would
report a ceiling the service cannot actually survive on a Monday morning.

WHAT THE NUMBER MEANS -- AND WHAT IT MISSES
-------------------------------------------
Run against `engine.provider: silent` this measures the TRANSPORT layer: accept,
UUID correlation, the pool, two OS threads per call, 20 ms write pacing and its
back-pressure, the record write, and teardown. **It is an upper bound, not a
prediction.**

Two things it deliberately does not see, and both matter when quoting the number:

1. **No providers.** No STT, LLM or TTS streams are opened, so the provider
   concurrency limit is untested. Real capacity is this number or the account
   limit, whichever is LOWER.

2. **No VAD, and therefore no burst cost.** The silent engine builds no
   pipeline, so it never constructs a `SileroVADAnalyzer` -- the one piece of
   per-call setup expensive enough to stall the event loop when several calls
   land together ([[decisions]] 047, still ~426 ms for eight at once). A spike
   test here is therefore *easier* than a real spike. The burst ceiling has to
   be measured with the real engine, at a level small enough to afford.

READING THE RESULT
------------------
The harness reports what it saw; the bot reports what it suffered. Trust the
bot's numbers -- they come from `/metrics`:

  * `frames_dropped` -- the pipeline could not keep up with a caller
  * `pacer_slips`    -- our audio reached callers late

**Any non-zero value means the ceiling was passed.** The last clean level is the
answer, not the level at which it fell over.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field

FRAME_BYTES = 320
FRAME_SECS = 0.020
TYPE_UUID = 0x01
TYPE_AUDIO = 0x10
TYPE_HANGUP = 0x00

# A quiet sine-ish payload rather than zeros: silence can be special-cased by
# codecs and VADs, and a load test should not accidentally take a cheaper path
# than a real call.
TONE = bytes(bytearray((i * 7) % 251 for i in range(FRAME_BYTES)))


def message(kind: int, payload: bytes) -> bytes:
    return bytes([kind, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) + payload


@dataclass
class CallerResult:
    frames_sent: int = 0
    frames_received: int = 0
    connected: bool = False
    error: str = ""
    # How late OUR OWN sends were. Reported so a struggling harness cannot be
    # mistaken for a struggling server -- if this is large, the numbers below
    # are about this machine, not the bot.
    worst_send_gap_ms: float = 0.0


@dataclass
class Report:
    callers: list[CallerResult] = field(default_factory=list)

    @property
    def connected(self) -> int:
        return sum(1 for c in self.callers if c.connected)

    @property
    def failed(self) -> list[str]:
        return [c.error for c in self.callers if c.error]


async def one_caller(host: str, port: int, duration: float) -> CallerResult:
    """One virtual caller: connect, announce, then 50 frames a second."""
    result = CallerResult()
    call_id = uuid.uuid4()
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError as e:
        result.error = f"connect failed: {e}"
        return result

    result.connected = True
    try:
        # The first message Asterisk sends is the UUID from externalMedia's
        # `data` field. Without it the bot waits 2 s and treats this as a
        # direct (non-ARI) call -- which is what we want, but sending it keeps
        # the timing identical to a real Stasis call.
        writer.write(message(TYPE_UUID, call_id.bytes))
        await writer.drain()

        async def drain_inbound():
            """Read continuously. AudioSocket is lockstep: a client that stops
            reading applies back-pressure the bot would never see from Asterisk,
            and the test would measure our own laziness."""
            try:
                while True:
                    header = await reader.readexactly(3)
                    length = (header[1] << 8) | header[2]
                    if length:
                        await reader.readexactly(length)
                    if header[0] == TYPE_AUDIO:
                        result.frames_received += 1
            except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
                return

        reading = asyncio.create_task(drain_inbound())

        deadline = time.monotonic() + duration
        next_send = time.monotonic()
        while time.monotonic() < deadline:
            writer.write(message(TYPE_AUDIO, TONE))
            await writer.drain()
            result.frames_sent += 1

            next_send += FRAME_SECS
            gap = next_send - time.monotonic()
            if gap > 0:
                await asyncio.sleep(gap)
            else:
                result.worst_send_gap_ms = max(result.worst_send_gap_ms, -gap * 1000)
                next_send = time.monotonic()  # fell behind; resync

        writer.write(message(TYPE_HANGUP, b""))
        await writer.drain()
        reading.cancel()
    except (ConnectionError, OSError) as e:
        result.error = f"dropped mid-call: {e}"
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
    return result


def metrics(api: str) -> dict:
    """Pull the bot's own counters. These are the authoritative numbers."""
    wanted = (
        "voiceagent_calls_total",
        "voiceagent_calls_rejected_total",
        "voiceagent_frames_dropped_total",
        "voiceagent_pacer_slips_total",
        "voiceagent_pool_busy",
    )
    try:
        with urllib.request.urlopen(f"{api}/metrics", timeout=2) as r:
            text = r.read().decode()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name, _, value = line.partition(" ")
        if name in wanted:
            out[name.replace("voiceagent_", "")] = float(value)
    return out


def summarise(label: str, report: Report, before: dict, after: dict) -> bool:
    """Print one level's result. Returns True if it was clean."""
    received = [c.frames_received for c in report.callers if c.connected]
    gaps = [c.worst_send_gap_ms for c in report.callers if c.connected]

    print(f"\n=== {label} ===")
    print(f"  connected            {report.connected}/{len(report.callers)}")
    if report.failed:
        for err in report.failed[:3]:
            print(f"  FAILED               {err}")
    if received:
        print(f"  frames back (median) {statistics.median(received):.0f}")
        print(f"  harness worst gap    {max(gaps):.1f} ms"
              + ("   <-- THE HARNESS is struggling, not the bot"
                 if max(gaps) > 50 else ""))

    def delta(key):
        return after.get(key, 0) - before.get(key, 0)

    if "error" in after:
        print(f"  bot metrics          unavailable ({after['error']})")
        return not report.failed

    dropped, slips = delta("frames_dropped_total"), delta("pacer_slips_total")
    rejected = int(delta("calls_rejected_total"))
    print(f"  bot: calls           +{delta('calls_total'):.0f}")
    print(f"  bot: frames dropped  +{dropped:.0f}")
    print(f"  bot: pacer slips     +{slips:.0f}")

    # A rejected caller is the pool working correctly, NOT the machine
    # struggling. Their socket is closed on purpose, so they also show up as
    # harness-side failures -- discount exactly that many before judging.
    unexplained = max(0, len(report.failed) - rejected)
    if rejected:
        print(f"  bot: REJECTED        +{rejected}  <-- POOL FULL, not a machine limit")
    if unexplained:
        print(f"  unexplained failures {unexplained}")

    if rejected and not dropped and not slips and not unexplained:
        print("  --> AT CAPACITY (raise pool.personas to test the machine)")
        return "capacity"

    clean = not dropped and not slips and not unexplained
    print(f"  --> {'CLEAN' if clean else 'DEGRADED'}")
    return clean


async def level(host, port, api, n, duration, label) -> bool:
    before = metrics(api)
    results = await asyncio.gather(
        *(one_caller(host, port, duration) for _ in range(n))
    )
    await asyncio.sleep(1)  # let the bot finish its teardown and record writes
    after = metrics(api)
    return summarise(label, Report(list(results)), before, after)


async def main(args) -> int:
    api = f"http://{args.api_host}:{args.api_port}"
    print(f"target      {args.host}:{args.port}   (metrics: {api})")
    probe = metrics(api)
    if "error" in probe:
        print(f"WARNING: cannot read {api}/metrics -- {probe['error']}")
        print("         Falling back to harness-side numbers only, which cannot")
        print("         see dropped frames or pacer slips. Start the bot first.")
    else:
        print(f"pool busy   {probe.get('pool_busy', 0):.0f} before we start")

    last_clean = 0
    capacity_bound = False

    if args.spike:
        ok = await level(args.host, args.port, api, args.spike, args.duration,
                         f"SPIKE {args.spike} callers at once")
        capacity_bound = ok == "capacity"
        last_clean = args.spike if ok is True else 0
    else:
        n = args.step
        while n <= args.ramp:
            ok = await level(args.host, args.port, api, n, args.duration,
                             f"RAMP {n} concurrent callers")
            if ok == "capacity":
                capacity_bound = True
                break
            if not ok:
                print(f"\nDegraded at {n}. Last clean level: {last_clean}.")
                break
            last_clean = n
            n += args.step
            await asyncio.sleep(args.every)

    print(f"\n{'=' * 62}")
    if capacity_bound:
        # The single most likely way to misread this tool: stopping at the
        # roster size and calling it the machine's ceiling.
        print("STOPPED AT THE CONFIGURED CAPACITY, not a machine limit.")
        print("The pool refused the extra callers exactly as it should. To find")
        print("what this MACHINE can do, raise pool.personas above the level you")
        print("want to test and run again -- with engine.provider: silent the")
        print("prompts and voices are never used, so copies are fine.")
    else:
        print(f"LAST CLEAN LEVEL: {last_clean} concurrent callers")
        print("")
        print("This is a TRANSPORT-layer upper bound. It does NOT include:")
        print("  * provider limits -- no STT/LLM/TTS streams were opened, so the")
        print("    Deepgram and Gemini concurrency caps are untested. Real")
        print("    capacity is this number or theirs, whichever is LOWER.")
        print("  * per-call VAD setup -- the silent engine builds no pipeline, so")
        print("    a spike here is EASIER than a real one. Burst arrival must be")
        print("    measured with the real engine.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--api-host", default="127.0.0.1")
    p.add_argument("--api-port", type=int, default=8091)
    p.add_argument("--ramp", type=int, default=12, help="ramp up to this many")
    p.add_argument("--step", type=int, default=3, help="callers added per level")
    p.add_argument("--every", type=float, default=3, help="seconds between levels")
    p.add_argument("--spike", type=int, help="instead: N callers all at once")
    p.add_argument("--duration", type=float, default=15, help="seconds per call")
    sys.exit(asyncio.run(main(p.parse_args())))
