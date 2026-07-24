"""
Phase 2: the AudioSocket bridge, wrapped as a Pipecat transport.

Pipecat ships transports for WebRTC/WebSockets -- none speak AudioSocket, so we
write ours. The AudioSocket protocol parsing is the same you wrote in Phase 1.

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

Verified against pipecat-ai 1.5.0.
"""

import asyncio
import queue
import socket
import struct
import threading
import time
import uuid

from loguru import logger
from pipecat.frames.frames import InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

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
SAMPLE_RATE = 8000
NUM_CHANNELS = 1
FRAME_BYTES = 320
FRAME_SECS = 0.02
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
        self.frames_dropped = 0
        self.frames_out = 0
        self.frames_out_real = 0

        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)
        except OSError:
            pass

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

    def _signal_end(self, reason: str):
        """Called from a thread; wakes the asyncio side."""
        if not self.hangup_event.is_set():
            self.end_reason = reason
            self._loop.call_soon_threadsafe(self.hangup_event.set)
            self._loop.call_soon_threadsafe(self.incoming.put_nowait, None)

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
                logger.info(f"Call id: {self.call_id}")
            except ValueError:
                pass
            # Wake anyone waiting to correlate this connection with an ARI call.
            self._loop.call_soon_threadsafe(self.uuid_ready.set)
        elif msg_type == TYPE_DTMF:
            logger.info(f"DTMF pressed: {chr(payload[0]) if payload else '?'}")
        elif msg_type == TYPE_HANGUP:
            self._signal_end("Asterisk sent HANGUP (caller hung up)")
        elif msg_type == TYPE_ERROR:
            logger.warning(f"Asterisk error, code {payload[0] if payload else -1}")

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
                    logger.info(
                        f"audio out: {self.frames_out} frames total "
                        f"({self.frames_out_real} real), +{heartbeat_frames} "
                        f"in last {now - last_heartbeat:.1f}s"
                    )
                    last_heartbeat = now
                    heartbeat_frames = 0

                next_send += FRAME_SECS
                gap = next_send - time.monotonic()
                if gap > 0:
                    time.sleep(gap)
                elif gap < -0.1:
                    next_send = time.monotonic()  # fell behind -- resync
        except Exception as e:  # noqa: BLE001
            self._signal_end(f"write loop crashed: {e!r}")

    def queue_output(self, payload: bytes):
        """Called from the asyncio side. Blocks (in a worker thread) when the
        outgoing queue is full -- that back-pressure is what paces the agent's
        speech to real time and keeps Pipecat's timing accurate."""
        self._outgoing.put(payload)

    def flush_output(self):
        """Drop any buffered agent audio (used on barge-in / interruption)."""
        try:
            while True:
                self._outgoing.get_nowait()
        except queue.Empty:
            pass


class AudioSocketInputTransport(BaseInputTransport):
    """Feeds queued caller audio into the pipeline."""

    def __init__(self, conn: AudioSocketConnection, params: TransportParams, **kwargs):
        super().__init__(params, **kwargs)
        self._conn = conn
        self._pump_task = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if not self._pump_task:
            self._pump_task = asyncio.create_task(self._pump_loop())
        await self.set_transport_ready(frame)

    async def _pump_loop(self):
        while True:
            payload = await self._conn.incoming.get()
            if payload is None:  # sentinel: call ended
                break
            await self.push_audio_frame(
                InputAudioRawFrame(
                    audio=payload, sample_rate=SAMPLE_RATE, num_channels=NUM_CHANNELS
                )
            )

    async def cleanup(self):
        await super().cleanup()
        if self._pump_task:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None


class AudioSocketOutputTransport(BaseOutputTransport):
    """Hands the agent's audio to the write thread (which paces + sends it)."""

    def __init__(self, conn: AudioSocketConnection, params: TransportParams, **kwargs):
        super().__init__(params, **kwargs)
        self._conn = conn

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        # The output queue is bounded, so put() blocks until the write thread has
        # room -- i.e. until real playback catches up. We run that blocking put
        # in a worker thread so it paces us without blocking the event loop.
        await asyncio.get_event_loop().run_in_executor(
            None, self._conn.queue_output, frame.audio
        )
        return True


class AudioSocketTransport(BaseTransport):
    """Bundles the input and output halves for one phone call."""

    def __init__(self, conn: AudioSocketConnection, params: TransportParams | None = None, **kwargs):
        super().__init__(**kwargs)
        self._conn = conn
        self._params = params or TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=SAMPLE_RATE,
            audio_in_channels=NUM_CHANNELS,
            audio_out_enabled=True,
            audio_out_sample_rate=SAMPLE_RATE,
            audio_out_channels=NUM_CHANNELS,
            audio_out_10ms_chunks=2,  # 20ms writes, matching AudioSocket frames
        )
        self._input = None
        self._output = None

    def input(self) -> FrameProcessor:
        if not self._input:
            self._input = AudioSocketInputTransport(self._conn, self._params)
        return self._input

    def output(self) -> FrameProcessor:
        if not self._output:
            self._output = AudioSocketOutputTransport(self._conn, self._params)
        return self._output
