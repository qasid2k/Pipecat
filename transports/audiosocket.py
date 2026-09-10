"""
The AudioSocket protocol and its threaded socket I/O.

Asterisk-specific and engine-agnostic: this module imports no Pipecat and knows
nothing about conversations. It is the plumbing underneath
`transports/asterisk.py`, which wraps it in a `CallSession`.

(The Pipecat glue that used to live here moved to `engine/session_transport.py`
in Phase 3, once it turned out to be engine code rather than transport code.)

WHY THREADS (the hard lesson): Asterisk's AudioSocket does non-blocking writes
with ZERO tolerance -- if it can't hand us a frame the instant it's ready, it
gives up and hangs up the call ("Resource temporarily unavailable" / connection
reset). Doing the socket I/O on the asyncio event loop is too fragile: the loop
stalls while loading ONNX models, during GC, and under inference load, and even
a brief stall lets Asterisk's TCP window fill and the call drops.

So socket I/O runs on DEDICATED OS THREADS with blocking recv()/sendall().
Blocking recv() releases the GIL while it waits, so we drain Asterisk instantly
and continuously no matter what the pipeline is doing. The threads bridge to the
async pipeline through queues.

Two other rules baked in:
  * Send audio CONTINUOUSLY -- silence when the agent isn't speaking. Asterisk's
    AudioSocket is lockstep; if we go quiet it stops forwarding the caller.
  * Pace output to real time (20ms/frame) so Asterisk doesn't drop a burst.

Verified against pipecat-ai 1.6.0 (pinned in requirements.txt).
"""

import asyncio
import queue
import socket
import struct
import threading
import time
import uuid

from loguru import logger

from core.transport import (
    CANONICAL_CHANNELS,
    CANONICAL_FRAME_BYTES,
    CANONICAL_FRAME_MS,
    CANONICAL_SAMPLE_RATE,
)

# ---------------------------------------------------------------------------
# AudioSocket protocol: [ 1 byte TYPE ][ 2 bytes LENGTH (big-endian) ][ PAYLOAD ]
# ---------------------------------------------------------------------------
TYPE_HANGUP = 0x00
TYPE_UUID = 0x01
TYPE_DTMF = 0x03
TYPE_AUDIO = 0x10
TYPE_ERROR = 0xFF

# 8 kHz, 16-bit signed, mono. One 20ms frame = 320 bytes. Deepgram + Silero VAD
# both handle 8 kHz, so we run the whole pipeline at 8 kHz (no resampling).
# These come from core.transport so the canonical format is defined ONCE and an
# adapter cannot quietly disagree with the core about it.
SAMPLE_RATE = CANONICAL_SAMPLE_RATE
NUM_CHANNELS = CANONICAL_CHANNELS
FRAME_BYTES = CANONICAL_FRAME_BYTES
FRAME_SECS = CANONICAL_FRAME_MS / 1000
SILENCE_FRAME = bytes(FRAME_BYTES)

# Cap the caller-audio backlog handed to the pipeline (drop oldest past this).
MAX_QUEUED_FRAMES = 100
# Bound the outgoing queue so write_audio_frame() gets natural back-pressure
# and stays paced to real playback (keeps Pipecat's bot-speaking timing honest).
MAX_OUT_FRAMES = 3
# Enlarge the OS receive buffer so a brief hiccup can't overflow it. Set on the
# LISTENING socket in bot.py (before bind) so window scaling is negotiated big;
# this is a belt-and-suspenders retry on the accepted socket.
RECV_BUFFER_BYTES = 1024 * 1024


# Windows' default timer granularity is ~15.6ms, so a 20ms sleep/queue-timeout
# rounds up to ~31ms -- which would pace our audio at ~1.6x too slow (laggy,
# choppy). Ask Windows for 1ms timer resolution so 20ms pacing is accurate.
if __import__("sys").platform == "win32":
    try:
        import ctypes

        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:  # noqa: BLE001
        pass


def build_message(msg_type: int, payload: bytes = b"") -> bytes:
    """Add the 3-byte AudioSocket header to a payload."""
    return struct.pack(">BH", msg_type, len(payload)) + payload


class AudioSocketConnection:
    """Owns the raw socket for one call and runs its two I/O threads.

    Read thread : blocking recv() -> parse frames -> hand caller audio to asyncio
    Write thread: pull agent audio (or silence) -> pace -> blocking sendall()
    """

    def __init__(self, sock: socket.socket, loop: asyncio.AbstractEventLoop):
        self._sock = sock
        self._loop = loop
        self._running = True

        # caller audio -> pipeline (lives on the asyncio side)
        self.incoming: asyncio.Queue = asyncio.Queue()
        # pipeline audio -> Asterisk (thread-safe; bounded for back-pressure)
        self._outgoing: queue.Queue = queue.Queue(maxsize=MAX_OUT_FRAMES)

        self._read_thread = None
        self._write_thread = None

        # diagnostics / lifecycle
        self.hangup_event = asyncio.Event()
        # Set once the AudioSocket UUID (first message) has been read, so the
        # async side can correlate this connection with its ARI call.
        self.uuid_ready = asyncio.Event()
        self.end_reason = "not ended"
        self.call_id = None
        self.frames_in = 0
        # Inbound frames thrown away because the pipeline could not keep up.
        # THE overload signal: anything above zero means this call was being
        # served worse than it should have been, and it is the number that
        # should stop a load test. It used to be incremented and never read
        # anywhere -- invisible by accident, not by design.
        self.frames_dropped = 0
        self.frames_out = 0
        self.frames_out_real = 0
        # Set by the write thread every time it takes a frame off _outgoing, so
        # queue_output() can wait on the event loop instead of parking a thread.
        # Starts set: the queue begins empty.
        self._space = asyncio.Event()
        self._space.set()
        # Times the 20 ms write pacer fell more than 100 ms behind and had to
        # resync. The outbound twin of frames_dropped: it means the agent's
        # speech was not delivered at real time, which the caller hears as
        # choppiness. Also previously detected and silently discarded.
        self.pacer_slips = 0

        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)
        except OSError:
            pass

    @property
    def log(self):
        """A logger carrying this call's id, for use from the I/O THREADS.

        `logger.contextualize()` in the call task binds call_id via a contextvar,
        and `threading.Thread` does not copy the caller's context -- so these two
        threads are outside it and would otherwise log unattributably. With three
        calls up, the heartbeats and DTMF lines interleave, and without this you
        cannot tell which call any of them belongs to.

        Bound per call rather than cached because `call_id` is not known when the
        connection is constructed: it arrives as the first AudioSocket message.
        """
        return logger.bind(call_id=str(self.call_id) if self.call_id else "-")

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self._read_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._write_thread = threading.Thread(target=self._write_loop, daemon=True)
        self._read_thread.start()
        self._write_thread.start()

    def stop(self):
        self._running = False
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        # The write thread is on its way out, so nothing will drain the outgoing
        # queue again. Release any waiter so it can see `_running` is False and
        # give up, rather than waiting for room that will never appear.
        self._signal_space()

    def _signal_space(self):
        """Tell the asyncio side there is room on the outgoing queue.

        Called from the WRITE THREAD, so it must go through the loop rather than
        touching the Event directly -- asyncio primitives are not thread-safe.
        `call_soon_threadsafe` is also correct when called from the loop itself,
        which is why `flush_output` can use the same path.
        """
        try:
            self._loop.call_soon_threadsafe(self._space.set)
        except RuntimeError:
            # The loop is closing; nothing is waiting on this any more.
            pass

    def _signal_end(self, reason: str):
        """Called from a thread; wakes the asyncio side.

        Wakes BOTH directions. The inbound sentinel alone is not enough: a call
        can be blocked on the OUTBOUND queue at the moment the socket dies, and
        the write thread that would normally free it has just stopped. Leaving
        that waiter asleep strands the call and leaks its persona ([[bugs]]
        B-014).
        """
        if not self.hangup_event.is_set():
            self.end_reason = reason
            self._loop.call_soon_threadsafe(self.hangup_event.set)
            self._loop.call_soon_threadsafe(self.incoming.put_nowait, None)
            # Wake anyone waiting for room; `queue_output` re-checks
            # hangup_event first thing and gives up.
            self._signal_space()

    # -- READ thread (blocking recv, never starved by the event loop) ------
    def _read_loop(self):
        buf = bytearray()
        try:
            while self._running:
                data = self._sock.recv(65536)
                if not data:
                    self._signal_end("socket closed by Asterisk (no HANGUP message)")
                    return
                buf.extend(data)
                # Extract every complete AudioSocket message in the buffer.
                while len(buf) >= 3:
                    length = (buf[1] << 8) | buf[2]
                    if len(buf) < 3 + length:
                        break  # wait for the rest of this message
                    msg_type = buf[0]
                    payload = bytes(buf[3 : 3 + length])
                    del buf[: 3 + length]
                    self._on_message(msg_type, payload)
        except OSError as e:
            self._signal_end(f"read socket error: {e!r}")
        except Exception as e:  # noqa: BLE001
            self._signal_end(f"read loop crashed: {e!r}")

    def _on_message(self, msg_type: int, payload: bytes):
        if msg_type == TYPE_AUDIO:
            self.frames_in += 1
            self._loop.call_soon_threadsafe(self._push_incoming, payload)
        elif msg_type == TYPE_UUID:
            try:
                self.call_id = uuid.UUID(bytes=payload)
                self.log.info(f"Call id: {self.call_id}")
            except ValueError:
                pass
            # Wake anyone waiting to correlate this connection with an ARI call.
            self._loop.call_soon_threadsafe(self.uuid_ready.set)
        elif msg_type == TYPE_DTMF:
            self.log.info(f"DTMF pressed: {chr(payload[0]) if payload else '?'}")
        elif msg_type == TYPE_HANGUP:
            self._signal_end("Asterisk sent HANGUP (caller hung up)")
        elif msg_type == TYPE_ERROR:
            self.log.warning(f"Asterisk error, code {payload[0] if payload else -1}")

    def _push_incoming(self, payload: bytes):
        """Runs on the event loop. Drop oldest if the pipeline falls behind."""
        if self.incoming.qsize() >= MAX_QUEUED_FRAMES:
            try:
                self.incoming.get_nowait()
                self.frames_dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.incoming.put_nowait(payload)

    # -- WRITE thread (paced sendall; silence when the agent is quiet) -----
    def _write_loop(self):
        # Pace off a monotonic clock (NOT the queue timeout, which is coarse on
        # Windows). Each tick: send one agent frame if queued, else silence,
        # then sleep to the next 20ms boundary.
        next_send = time.monotonic()
        last_heartbeat = next_send
        heartbeat_frames = 0
        try:
            while self._running:
                try:
                    payload = self._outgoing.get_nowait()
                    is_real = True
                    # Room has appeared. Wake whoever is waiting to hand us the
                    # next frame -- this is what replaces the blocking put() and
                    # the thread-pool worker it used to occupy.
                    self._signal_space()
                except queue.Empty:
                    payload = SILENCE_FRAME  # keep Asterisk's lockstep loop alive
                    is_real = False
                try:
                    self._sock.sendall(build_message(TYPE_AUDIO, payload))
                except OSError as e:
                    self._signal_end(f"write socket error: {e!r}")
                    return
                self.frames_out += 1
                heartbeat_frames += 1
                if is_real:
                    self.frames_out_real += 1

                now = time.monotonic()
                if now - last_heartbeat >= 5.0:
                    # Proves whether we are ACTUALLY sending continuously, in
                    # the real Asterisk environment -- not just in a unit test.
                    # Drops and slips are included so overload is visible while
                    # the call is still happening, not only in the closing line.
                    self.log.info(
                        f"audio out: {self.frames_out} frames total "
                        f"({self.frames_out_real} real), +{heartbeat_frames} "
                        f"in last {now - last_heartbeat:.1f}s"
                        + (
                            f" | DROPPED {self.frames_dropped} in,"
                            f" {self.pacer_slips} pacer slips"
                            if (self.frames_dropped or self.pacer_slips)
                            else ""
                        )
                    )
                    last_heartbeat = now
                    heartbeat_frames = 0

                next_send += FRAME_SECS
                gap = next_send - time.monotonic()
                if gap > 0:
                    time.sleep(gap)
                elif gap < -0.1:
                    # More than 100 ms behind: this call's audio has already been
                    # delivered late. Count it -- resyncing silently is how a
                    # machine at its limit goes on looking healthy.
                    self.pacer_slips += 1
                    next_send = time.monotonic()  # fell behind -- resync
        except Exception as e:  # noqa: BLE001
            self._signal_end(f"write loop crashed: {e!r}")

    async def queue_output(self, payload: bytes) -> None:
        """Hand one frame to the write thread, waiting if it is behind.

        THE BACK-PRESSURE IS THE POINT, and it must survive any change here.
        The outgoing queue holds 3 frames = 60 ms. When it is full this waits,
        which is what paces the agent's speech to real time and keeps Pipecat's
        timing honest ([[decisions]] 002). Dropping frames instead, or growing
        the queue, would let the agent "speak" faster than the caller can hear.

        WHY THIS IS NOT `run_in_executor(None, queue.put)` ANY MORE
        ----------------------------------------------------------
        It used to be, and that was the first hard capacity wall in the service.
        A blocking `put` in the default thread pool occupies a worker for as
        long as it waits -- up to 20 ms, every frame, 50 times a second, per
        call. The pool is `min(32, cpu+4)` threads and is shared with everything
        else that uses `run_in_executor`, so at a few tens of concurrent calls
        the workers are all parked in `put()` and unrelated work starves behind
        them. It also cost a queue hop and two context switches per frame for
        what is, in the common case, one non-blocking append.

        Now the waiting happens on the event loop: the write thread sets
        `_space` every time it consumes a frame, so this sleeps until there is
        genuinely room and costs nothing while it waits. No thread, no pool, no
        ceiling that arrives at 32.
        """
        while True:
            # THE CALL IS OVER CHECK, FIRST AND EVERY TIME ROUND.
            #
            # Only the write thread makes room, so once it has stopped -- the
            # caller hung up, the socket died -- nothing will ever drain this
            # queue again. Without this check a writer that arrives while the
            # queue is full waits on `_space` forever, `engine.run()` never
            # returns, and that call's PERSONA IS NEVER RELEASED. Capacity then
            # drops by one, silently and permanently.
            #
            # Found by the load harness: 60 agents held with no calls in
            # progress ([[bugs]] B-014). Dropping the frame is right -- the call
            # is over and this audio has nowhere to go.
            if not self._running or self.hangup_event.is_set():
                return

            # Cleared BEFORE the attempt, not after a failure. If the write
            # thread consumes between a failed put and the wait, the set() would
            # land on an event we then cleared, and this frame would sit here
            # until the next consume 20 ms later. Clearing first makes that
            # ordering impossible.
            self._space.clear()
            try:
                self._outgoing.put_nowait(payload)
                return
            except queue.Full:
                await self._space.wait()

    def flush_output(self):
        """Drop any buffered agent audio (for barge-in / interruption).

        Not currently wired to anything -- the queue is only 3 frames, so an
        interruption loses at most 60 ms of already-committed audio, which is
        below what a caller notices. Kept because it is the right hook if
        barge-in ever needs to be tighter.
        """
        try:
            while True:
                self._outgoing.get_nowait()
        except queue.Empty:
            pass
        # Emptying the queue is exactly the condition a waiting writer is
        # blocked on; forgetting this would hang the next frame for 20 ms.
        self._signal_space()

