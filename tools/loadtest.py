"""Find this machine's call ceiling by pretending to be Asterisk.

Each virtual caller opens an AudioSocket connection, announces a UUID, then
sends a 320-byte frame every 20 ms and drains whatever comes back -- exactly
what Asterisk does on extension 6000. The bot cannot tell the difference.

    # transport ceiling -- silent engine, free
    python tools/loadtest.py --write-config config.local.yaml
    python bot.py config.local.yaml                  # one terminal
    python tools/loadtest.py --ramp 30 --step 3      # another
    python tools/loadtest.py --spike 20 --duration 30

    # conversation ceiling -- REAL engine, costs money, start small
    python tools/loadtest.py --write-config config.speech.yaml --real
    python bot.py config.speech.yaml
    python tools/loadtest.py --speech --allow-real-engine --spike 2

SPEECH MODE
-----------
A tone never trips the voice detector, so against the real engine it measures
nothing: STT, the LLM and TTS never run. `--speech` makes each caller wait for
the greeting, say a real sentence (Deepgram TTS, generated once and cached),
then time how long until the agent starts answering. That TURN LATENCY is the
ceiling that matters for a voice agent -- callers hang up on a slow agent long
before any frame is dropped. A level fails when its p95 exceeds --max-latency.
It includes the silence timeout (engine.turn_taking.silence_timeout_s), because
the caller has to wait through that too.

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
import hashlib
import json
import math
import os
import statistics
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

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


SILENCE = bytes(FRAME_BYTES)


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
    # Speech mode only. What a CALLER experiences, which is the ceiling that
    # matters for a voice agent: long before frames drop, replies get too slow
    # to feel like a conversation.
    greeting_s: float | None = None   # connect -> agent's first audio
    turn_s: float | None = None       # caller stops talking -> agent answers


class _Heard:
    """What the drain loop has heard from the agent, for the controller to act
    on. The bot's idle keep-alive is exactly 320 zero bytes, so ANY non-zero
    frame is the agent speaking -- a clean signal, no audio analysis needed."""

    def __init__(self):
        self.first_audio: float | None = None
        self.last_audio: float | None = None
        self.waiting_since: float | None = None
        self.reply_at: float | None = None

    def on_frame(self, payload: bytes) -> None:
        if payload == SILENCE or not any(payload):
            return
        now = time.monotonic()
        if self.first_audio is None:
            self.first_audio = now
        self.last_audio = now
        if self.waiting_since is not None and self.reply_at is None:
            self.reply_at = now


@dataclass
class Report:
    callers: list[CallerResult] = field(default_factory=list)

    @property
    def connected(self) -> int:
        return sum(1 for c in self.callers if c.connected)

    @property
    def failed(self) -> list[str]:
        return [c.error for c in self.callers if c.error]


def speech_frames(text: str, voice: str = "aura-2-helena-en") -> list[bytes]:
    """A real spoken sentence, as 20 ms frames of 8 kHz 16-bit mono PCM.

    Generated ONCE with Deepgram's text-to-speech and cached next to this file
    (git-ignored, *.pcm), so a load run does not pay for it again. Deepgram
    rather than a bundled recording because it is already a dependency, the key
    is already in .env on both machines, and it produces exactly the format the
    bot expects -- 8 kHz linear16, no container -- with no conversion step.

    One sentence of TTS costs a fraction of a cent. The load run that uses it is
    what costs money: see --allow-real-engine.
    """
    digest = hashlib.sha1(f"{voice}|{text}".encode()).hexdigest()[:10]
    cache = Path(__file__).resolve().parent / f".speech-{digest}.pcm"

    if not cache.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        except ImportError:
            pass
        key = os.getenv("DEEPGRAM_API_KEY")
        if not key:
            raise SystemExit(
                "Speech mode needs DEEPGRAM_API_KEY (in .env) once, to generate "
                "the sentence the virtual callers will say."
            )
        request = urllib.request.Request(
            "https://api.deepgram.com/v1/speak"
            f"?model={voice}&encoding=linear16&sample_rate=8000&container=none",
            data=json.dumps({"text": text}).encode(),
            headers={"Authorization": f"Token {key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as r:
                pcm = r.read()
        except Exception as e:  # noqa: BLE001
            raise SystemExit(f"Could not generate the speech sample: {e}") from e
        if len(pcm) < FRAME_BYTES * 10:
            raise SystemExit(f"Deepgram returned only {len(pcm)} bytes of audio.")
        cache.write_bytes(pcm)
        # 8000 samples/s x 2 bytes = 16000 bytes per second of audio.
        print(f"speech      generated and cached ({len(pcm) / 16000:.1f}s of audio)")
    pcm = cache.read_bytes()

    frames = [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
    frames[-1] = frames[-1].ljust(FRAME_BYTES, b"\x00")  # pad the final frame
    return frames


async def _wait_for(predicate, timeout: float) -> bool:
    """Poll a condition every 20 ms. True if it came true before the timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(FRAME_SECS)
    return predicate()


async def one_caller(
    host: str, port: int, duration: float, speech: list[bytes] | None = None
) -> CallerResult:
    """One virtual caller.

    Two modes:

      * TONE (default) -- a steady tone for `duration` seconds. Exercises the
        transport; the real engine's voice detector never fires on it, so STT,
        the LLM and TTS never run. Right for the silent engine, meaningless for
        the real one.
      * SPEECH -- wait for the greeting, say a real sentence, stay silent, and
        time how long the agent takes to start answering. The only mode that
        drives the whole pipeline, and the only one that measures what a caller
        actually feels.
    """
    result = CallerResult()
    call_id = uuid.uuid4()
    heard = _Heard()
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError as e:
        result.error = f"connect failed: {e}"
        return result

    result.connected = True
    connected_at = time.monotonic()
    done = asyncio.Event()
    # Frames queued to be spoken. The sender plays these, then silence -- never
    # tone -- so the voice detector does not hear a "caller" talking over the
    # greeting and barge in on it.
    outbox: list[bytes] = []

    async def drain_inbound():
        """Read continuously. AudioSocket is lockstep: a client that stops
        reading applies back-pressure the bot would never see from Asterisk,
        and the test would measure our own laziness."""
        try:
            while True:
                header = await reader.readexactly(3)
                length = (header[1] << 8) | header[2]
                payload = await reader.readexactly(length) if length else b""
                if header[0] == TYPE_AUDIO:
                    result.frames_received += 1
                    heard.on_frame(payload)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            return

    async def send_paced():
        """One frame every 20 ms, for the life of the call. Asterisk never
        stops sending, so neither do we."""
        next_send = time.monotonic()
        while not done.is_set():
            if outbox:
                frame = outbox.pop(0)
            else:
                frame = TONE if speech is None else SILENCE
            writer.write(message(TYPE_AUDIO, frame))
            await writer.drain()
            result.frames_sent += 1

            next_send += FRAME_SECS
            gap = next_send - time.monotonic()
            if gap > 0:
                await asyncio.sleep(gap)
            else:
                result.worst_send_gap_ms = max(result.worst_send_gap_ms, -gap * 1000)
                next_send = time.monotonic()  # fell behind; resync

    try:
        # The first message Asterisk sends is the UUID from externalMedia's
        # `data` field. Without it the bot waits 2 s and treats this as a
        # direct (non-ARI) call -- which is what we want, but sending it keeps
        # the timing identical to a real Stasis call.
        writer.write(message(TYPE_UUID, call_id.bytes))
        await writer.drain()

        reading = asyncio.create_task(drain_inbound())
        sending = asyncio.create_task(send_paced())

        if speech is None:
            await asyncio.sleep(duration)
        else:
            await _converse(heard, outbox, speech, connected_at, result)

        done.set()
        await sending
        writer.write(message(TYPE_HANGUP, b""))
        await writer.drain()
        reading.cancel()
    except (ConnectionError, OSError) as e:
        result.error = f"dropped mid-call: {e}"
    finally:
        done.set()
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
    return result


async def _converse(heard: _Heard, outbox: list, speech: list[bytes],
                    connected_at: float, result: CallerResult) -> None:
    """The scripted half of a speech-mode call."""
    # 1. The greeting. Its start is time-to-answer; its end is when the agent
    #    stops talking, which is when a real caller would reply. A second of
    #    unbroken silence ends it -- longer than the pauses between sentences.
    if not await _wait_for(lambda: heard.first_audio is not None, timeout=20):
        result.error = "no greeting within 20 s"
        return
    result.greeting_s = heard.first_audio - connected_at
    await _wait_for(
        lambda: time.monotonic() - heard.last_audio > 1.0, timeout=20
    )

    # 2. Say the sentence, then go quiet.
    outbox.extend(speech)
    await _wait_for(lambda: not outbox, timeout=30)
    spoke_at = time.monotonic()
    heard.waiting_since = spoke_at

    # 3. Time the answer. This INCLUDES the voice detector's silence timeout
    #    (engine.turn_taking.silence_timeout_s) -- the agent cannot know the
    #    caller has finished until they have been quiet that long -- so it is
    #    the latency the caller actually hears, not the model's alone.
    if not await _wait_for(lambda: heard.reply_at is not None, timeout=20):
        result.error = "no reply within 20 s of speaking"
        return
    result.turn_s = heard.reply_at - spoke_at

    # 4. Let the reply finish, so TTS is exercised to the end rather than cut.
    await _wait_for(lambda: time.monotonic() - heard.last_audio > 1.0, timeout=20)


def health(api: str) -> dict:
    try:
        with urllib.request.urlopen(f"{api}/health", timeout=2) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def metrics(api: str) -> dict:
    """Pull the bot's own counters. These are the authoritative numbers."""
    wanted = (
        "voiceagent_calls_total",
        "voiceagent_calls_rejected_total",
        "voiceagent_frames_dropped_total",
        "voiceagent_pacer_slips_total",
        "voiceagent_pool_busy",
        # What each call costs the machine. Standard Prometheus process metrics;
        # RSS is Linux-only and simply absent elsewhere.
        "process_cpu_seconds_total",
        "process_resident_memory_bytes",
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


def pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile. statistics.quantiles needs n >= 2 and
    interpolates; with a handful of callers the honest answer is a real
    observed value, not an invented one between two of them."""
    ordered = sorted(values)
    rank = max(1, math.ceil(p * len(ordered)))
    return ordered[rank - 1]


def resources(before: dict, end: dict, wall: float, peak_rss, n: int) -> None:
    """CPU and memory for this level -- what a call costs the machine."""
    cpu0 = before.get("process_cpu_seconds_total")
    cpu1 = end.get("process_cpu_seconds_total")
    if cpu0 is not None and cpu1 is not None and wall > 0:
        # 100% = one core fully busy. Can exceed 100 on a multi-core box, since
        # the two I/O threads per call run outside the event loop.
        cpu_pct = (cpu1 - cpu0) / wall * 100
        print(f"  bot: CPU             {cpu_pct:.0f}% of one core"
              f"  ({cpu_pct / max(n, 1):.1f}% per call)")
    idle = before.get("process_resident_memory_bytes")
    if idle is None:
        print("  bot: memory          n/a (the bot reports it on Linux only)")
    elif peak_rss is not None:
        mb = 1024 * 1024
        per_call = (peak_rss - idle) / max(n, 1) / mb
        print(f"  bot: memory          {peak_rss / mb:.0f} MB peak"
              f"  (+{per_call:.1f} MB per call over idle)")


def latency(report: Report, max_latency: float) -> bool:
    """Speech mode: what the caller hears. Returns True if within budget."""
    greets = [c.greeting_s for c in report.callers if c.greeting_s is not None]
    turns = [c.turn_s for c in report.callers if c.turn_s is not None]
    if greets:
        print(f"  greeting             p50 {pct(greets, .5):.2f}s   "
              f"p95 {pct(greets, .95):.2f}s")
    if not turns:
        print("  turn latency         NO REPLIES -- the agent never answered")
        return False
    p95 = pct(turns, .95)
    print(f"  turn latency         p50 {pct(turns, .5):.2f}s   p95 {p95:.2f}s"
          + (f"   <-- over the {max_latency:.1f}s budget" if p95 > max_latency else ""))
    return p95 <= max_latency


def summarise(label: str, report: Report, before: dict, after: dict,
              end: dict | None = None, wall: float = 0.0, peak_rss=None,
              speech: bool = False, max_latency: float = 3.0) -> bool:
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
    resources(before, end or after, wall, peak_rss, report.connected - rejected)
    fast_enough = latency(report, max_latency) if speech else True

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

    # In speech mode a level can be perfectly clean at the transport and still
    # be a failure: if the agent takes four seconds to answer, callers hang up.
    # For a voice agent that is the real ceiling, and it arrives first.
    clean = not dropped and not slips and not unexplained and fast_enough
    if not fast_enough and not dropped and not slips:
        print("  --> TOO SLOW (transport fine; the agent cannot keep up)")
        return False
    print(f"  --> {'CLEAN' if clean else 'DEGRADED'}")
    return clean


async def level(host, port, api, n, duration, label,
                speech: list[bytes] | None = None, max_latency: float = 3.0) -> bool:
    before = metrics(api)
    peak = {"rss": before.get("process_resident_memory_bytes")}
    stop = asyncio.Event()

    async def sample_memory():
        """Peak memory is only visible while calls are live, so sample during
        the level. In a thread: urllib blocks, and a blocked harness loop would
        delay our own 20 ms sends and show up as a fake server problem."""
        while not stop.is_set():
            m = await asyncio.to_thread(metrics, api)
            rss = m.get("process_resident_memory_bytes")
            if rss is not None:
                peak["rss"] = max(peak["rss"] or 0, rss)
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    sampler = asyncio.create_task(sample_memory())
    started = time.monotonic()
    results = await asyncio.gather(
        *(one_caller(host, port, duration, speech) for _ in range(n))
    )
    wall = time.monotonic() - started
    # CPU is read the moment the calls finish, before the teardown pause, so the
    # idle second afterwards does not dilute it.
    end = await asyncio.to_thread(metrics, api)
    stop.set()
    await sampler

    await asyncio.sleep(1)  # let the bot finish its teardown and record writes
    after = metrics(api)
    return summarise(label, Report(list(results)), before, after,
                     end=end, wall=wall, peak_rss=peak["rss"],
                     speech=speech is not None, max_latency=max_latency)


def write_config(path: str, personas: int, force: bool = False,
                 real: bool = False) -> int:
    """Derive a load-test config from the real one.

    Four changes, each of which is easy to forget by hand and expensive to
    forget in a different way:

      * `engine.provider: silent` -- otherwise every virtual caller opens real
        Deepgram and Gemini streams and the run costs money. (`real=True`
        keeps the real engine, for --speech: that is the point of it.)
      * a roster of `personas` -- if the pool is smaller than the test level you
        measure the pool, not the machine.
      * records and logs to separate files -- so a load run does not bury real
        call records under thousands of synthetic ones.
      * ARI commented out -- a load test drives AudioSocket directly and does
        not need call control, so this also runs on a machine without it.
    """
    import re

    source = Path(__file__).resolve().parent.parent / "config.yaml"
    text = source.read_text(encoding="utf-8")

    if not real:
        text = text.replace("provider: pipecat", "provider: silent")
    text = re.sub(r"^(\s*)ari_pass_env:", r"\1# ari_pass_env:", text, flags=re.M)
    text = text.replace("path: records/calls.db", "path: records/loadtest.db")
    text = text.replace("file: logs/agent.jsonl", "file: logs/loadtest.jsonl")

    if real:
        # The real engine DOES read the prompt and speak the voice, so cycle
        # the actual personas: a load test that ran every call as Alex would
        # measure one prompt length and one voice, not the service. Names must
        # be unique, so each repeat gets a number (it is spoken in the
        # greeting -- harmless for a test).
        real_personas = re.findall(
            r"- name: (\S+)\n\s+voice: (\S+)\n(?:\s*#.*\n)*\s+system_prompt_file: (\S+)",
            text,
        )
        if not real_personas:
            print("Could not read the personas from config.yaml's pool block.")
            return 1
        roster = "\n".join(
            f"    - name: {name} {i // len(real_personas) + 1}\n"
            f"      voice: {voice}\n"
            f"      system_prompt_file: {prompt}"
            for i, (name, voice, prompt) in (
                (i, real_personas[i % len(real_personas)]) for i in range(personas)
            )
        )
    else:
        # The silent engine never reads a prompt or speaks a voice, so
        # identical copies are fine -- these only make the pool big enough.
        roster = "\n".join(
            f"    - name: Load{i:02d}\n"
            f"      voice: aura-2-helena-en\n"
            f"      system_prompt_file: prompts/alex.txt"
            for i in range(personas)
        )
    text, count = re.subn(
        r"(pool:\n  personas:\n).*?(\n\n  # Each persona)",
        lambda m: m.group(1) + roster + m.group(2),
        text,
        flags=re.S,
    )
    if not count:
        print("Could not find the pool.personas block in config.yaml -- has its")
        print("shape changed? Edit the generated file by hand.")
        return 1

    out = Path(path)
    if out.exists() and not force:
        # Loud, because carrying on with a STALE config.local.yaml is the
        # expensive mistake: it may still say `pipecat`, in which case the load
        # run opens real provider streams and bills for them.
        print(f"!! {out} ALREADY EXISTS and was NOT changed.")
        print("")
        print("   If it is an old copy it probably still says engine.provider:")
        print("   pipecat -- running a load test against that opens a REAL")
        print("   Deepgram and Gemini stream per virtual caller, and costs money.")
        print("")
        print("   Overwrite it:")
        print(f"       python {sys.argv[0]} --write-config {out} --force")
        print("   Or keep it and write elsewhere:")
        print(f"       python {sys.argv[0]} --write-config config.loadtest.yaml")
        return 1
    out.write_text(text, encoding="utf-8")

    print(f"Wrote {out}:")
    if real:
        print(f"  engine.provider  pipecat  (REAL -- every call costs money)")
    else:
        print(f"  engine.provider  silent   (no Deepgram, no Gemini, no cost)")
    print(f"  pool.personas    {personas}        (so the pool is not the limit)")
    print(f"  records / logs   *loadtest*  (kept apart from real calls)")
    print(f"  ARI              disabled")
    print(f"\nIt is git-ignored. Now:")
    print(f"    python bot.py {out}")
    if real:
        print(f"    python {sys.argv[0]} --speech --allow-real-engine --spike 2")
    return 0


async def reachable(host: str, port: int) -> str:
    """Is anything listening? One connection, opened and closed."""
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError as e:
        return str(e)
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass
    return ""


async def main(args) -> int:
    if args.write_config:
        return write_config(args.write_config, args.personas, args.force,
                            real=args.real)

    api = f"http://{args.api_host}:{args.api_port}"
    print(f"target      {args.host}:{args.port}   (metrics: {api})")

    # Pre-flight. Running the whole ramp against a closed port and then printing
    # "LAST CLEAN LEVEL: 0" would be reporting a measurement that never happened
    # -- worse than failing, because it looks like an answer.
    #
    # Ask the control plane first. A bare socket probe IS a call as far as the
    # bot is concerned: it takes a persona and, on the real engine, builds a
    # whole pipeline -- still running when the first level starts, skewing it.
    # The API starts only after the AudioSocket listener, so a healthy /health
    # already proves the port is open.
    state = health(api)
    problem = "" if "error" not in state else await reachable(args.host, args.port)
    if problem:
        print(f"\nNothing is listening on {args.host}:{args.port} -- {problem}\n")
        print("The bot is not running. Start it first, in another terminal:")
        print("    python bot.py config.local.yaml")
        print("")
        print("No load config yet? Generate one:")
        print(f"    python {sys.argv[0]} --write-config config.local.yaml")
        return 1

    # Which engine is actually answering? A stale config.local.yaml that still
    # says `pipecat` looks identical from here until the bill arrives: every
    # virtual caller would open a real Deepgram and Gemini stream. Refusing by
    # default costs a re-run; not refusing costs money.
    engine = state.get("engine", "unknown")

    # Speech mode is the one case that NEEDS the real engine: the silent engine
    # never greets and never answers, so every caller would time out and the
    # run would report a failure that is really a misconfiguration.
    if args.speech and "error" not in state and engine == "silent":
        print("\n--speech needs the REAL engine, but the bot is running 'silent'.")
        print("The silent engine never greets and never replies, so every caller")
        print("would time out. Generate a real-engine load config and use that:")
        print(f"    python {sys.argv[0]} --write-config config.speech.yaml --real")
        print("    python bot.py config.speech.yaml")
        return 1

    if "error" not in state and engine != "silent":
        print(f"\nThe bot is running engine.provider = '{engine}', not 'silent'.")
        print("")
        print("Every virtual caller will open a REAL provider stream -- Deepgram")
        print("and Gemini -- and this run will cost money.")
        if args.speech:
            # Expected here, so no "stale config" diagnosis -- just the bill.
            calls = args.spike or sum(range(args.step, args.ramp + 1, args.step))
            print(f"This run places up to {calls} real calls of roughly 15 s each.")
            print("Start small (--spike 2), and raise it once the numbers make sense.")
        else:
            print(f"It would also measure the pool ({state.get('capacity', '?')} "
                  "agents), not the machine.")
            print("")
            print("Most likely a stale config.local.yaml. Regenerate it:")
            print("    mv config.local.yaml config.local.yaml.bak")
            print(f"    python {sys.argv[0]} --write-config config.local.yaml")
            print("    python bot.py config.local.yaml")
        print("")
        print("To go ahead against the REAL engine on purpose, pass")
        print("    --allow-real-engine")
        if not args.allow_real_engine:
            return 1
        print("\n--allow-real-engine given; continuing against a real engine.\n")

    probe = metrics(api)
    if "error" in probe:
        print(f"WARNING: cannot read {api}/metrics -- {probe['error']}")
        print("         AudioSocket is up but the control plane is not, so this")
        print("         run cannot see dropped frames or pacer slips -- the only")
        print("         reliable overload signals. Results will be weak evidence.")
        print(f"         Check service.api in the config, or pass --api-port.")
    else:
        print(f"pool busy   {probe.get('pool_busy', 0):.0f} before we start")

    speech = None
    if args.speech:
        speech = speech_frames(args.say)
        print(f"speech      \"{args.say}\" ({len(speech) * FRAME_SECS:.1f}s)")
        print(f"budget      p95 turn latency <= {args.max_latency:.1f}s")

    last_clean = 0
    capacity_bound = False

    if args.spike:
        ok = await level(args.host, args.port, api, args.spike, args.duration,
                         f"SPIKE {args.spike} callers at once",
                         speech=speech, max_latency=args.max_latency)
        capacity_bound = ok == "capacity"
        last_clean = args.spike if ok is True else 0
    else:
        n = args.step
        while n <= args.ramp:
            ok = await level(args.host, args.port, api, n, args.duration,
                             f"RAMP {n} concurrent callers",
                             speech=speech, max_latency=args.max_latency)
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
    elif speech is not None:
        print(f"LAST CLEAN LEVEL: {last_clean} concurrent conversations")
        print("")
        print("Measured with the REAL engine end to end -- VAD, Deepgram STT,")
        print("Gemini, Deepgram TTS -- and judged by what a caller hears: p95 turn")
        print(f"latency within {args.max_latency:.1f}s. Caveats when quoting it:")
        print("  * every caller says the SAME sentence, once. Real calls are longer")
        print("    and vary; treat this as an upper bound on conversations.")
        print("  * provider limits are per ACCOUNT. Anything else using the same")
        print("    keys -- including real calls right now -- shares them.")
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
    p.add_argument("--duration", type=float, default=15,
                   help="seconds per call (tone mode; --speech calls end when "
                        "the reply does)")
    p.add_argument(
        "--write-config", metavar="PATH",
        help="generate a load-test config from config.yaml and exit",
    )
    p.add_argument(
        "--personas", type=int, default=60,
        help="roster size for --write-config (must exceed the test level)",
    )
    p.add_argument(
        "--allow-real-engine", action="store_true",
        help="run against a non-silent engine. THIS COSTS MONEY: every virtual "
             "caller opens a real Deepgram and Gemini stream.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="with --write-config, overwrite an existing file",
    )
    p.add_argument(
        "--real", action="store_true",
        help="with --write-config, keep the REAL engine and cycle the real "
             "personas -- the config --speech needs",
    )
    p.add_argument(
        "--speech", action="store_true",
        help="callers say a real sentence and time the agent's reply. Needs "
             "the real engine, so also --allow-real-engine. COSTS MONEY.",
    )
    p.add_argument(
        "--say", default="Hi, what are your opening hours?",
        help="the sentence each --speech caller says",
    )
    p.add_argument(
        "--max-latency", type=float, default=3.0,
        help="--speech: p95 turn latency (s) above which a level fails",
    )
    sys.exit(asyncio.run(main(p.parse_args())))
