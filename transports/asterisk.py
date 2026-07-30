"""
Phase 2: the Asterisk transport -- our existing, working call path, moved behind
the CallSession / BaseTransport contracts with its behaviour byte-for-byte intact.

This adapter also covers FreePBX: FreePBX is a management UI on top of Asterisk,
and ARI, Stasis and AudioSocket are identical underneath. There is deliberately
no separate FreePBX adapter.

WHAT LIVES HERE
    AsteriskTransport    the listening TCP socket + accept thread, the ARI
                         Stasis app, the mixing bridge / External Media setup,
                         and the UUID correlation that pairs an incoming
                         AudioSocket connection with its ARI caller channel to
                         produce ONE CallSession.
    AsteriskCallSession  one call: the AudioSocket connection, exposed as async
                         read_audio()/write_audio(), plus transfer() and
                         hangup() implemented the ARI way.

THE THREAD <-> ASYNCIO BRIDGE (the trickiest part of the codebase -- read this)
------------------------------------------------------------------------------
The socket I/O deliberately does NOT run on the asyncio event loop. Asterisk's
AudioSocket writer is non-blocking with zero tolerance: if it cannot hand us a
frame the instant it is ready, it abandons the call. Any event-loop stall (ONNX
model load, GC, LLM/TTS inference) was long enough to do exactly that. So two
dedicated OS threads per call do blocking recv()/sendall(), and they hand off to
asyncio through queues. That machinery already exists in AudioSocketConnection
and is UNCHANGED by this refactor -- we only put an async face on it:

    read_audio()   ->  await self._io.incoming.get()
                       `incoming` is an asyncio.Queue. The READ THREAD never
                       touches it directly; it calls loop.call_soon_threadsafe,
                       which is the only safe way to reach asyncio from a
                       thread. So awaiting it here is ordinary asyncio -- the
                       thread boundary was already crossed for us.

    write_audio()  ->  run_in_executor(None, self._io.queue_output, pcm)
                       `_outgoing` is a thread-safe queue.Queue bounded to 3
                       frames, so queue_output() BLOCKS when the write thread
                       has not drained it yet. That block is not a bug -- it is
                       what paces the agent's speech to real time. We run it in
                       an executor thread so it paces us without freezing the
                       event loop.

Two rules that must survive any future edit:
  * The write thread sends CONTINUOUSLY, emitting silence when the agent is
    quiet. AudioSocket is lockstep; if we go quiet, Asterisk stops forwarding
    the caller's audio and the call goes deaf both ways.
  * Pacing is off a monotonic clock at 20 ms/frame. Do not replace it with
    sleeps or queue timeouts (they round to ~31 ms on Windows).

Verified against pipecat-ai 1.6.0 (this module itself imports no Pipecat).
"""

import asyncio
import os
import socket
import threading
from typing import AsyncIterator

from loguru import logger

from ari_controller import AriCall, AriController
from core.transport import BaseTransport, CallSession
from transports.audiosocket import RECV_BUFFER_BYTES, AudioSocketConnection

# How long to wait for the AudioSocket UUID before deciding this is a direct
# (non-ARI) call. The UUID is the FIRST message Asterisk sends, so this is
# generous; it exists so a direct call to extension 6000 -- which never gets a
# UUID from ARI -- proceeds instead of hanging.
UUID_CORRELATION_TIMEOUT_S = 2.0


class AsteriskCallSession(CallSession):
    """One Asterisk call: an AudioSocket audio path, plus ARI for control.

    Two flavours arrive here, and the difference matters:

      * **Stasis call (extension 6001)** -- has an `AriCall`, so it has a caller
        channel we can act on. Transfer works. This is the real path.
      * **Direct AudioSocket call (extension 6000)** -- no ARI channel at all.
        Audio and conversation work; `can_transfer` is False.
    """

    def __init__(
        self,
        io: AudioSocketConnection,
        addr,
        controller: AriController | None = None,
        ari_call: AriCall | None = None,
        transfer_context: str = "transfer",
    ):
        self._io = io
        self._addr = addr
        self._controller = controller
        self._ari_call = ari_call
        self._transfer_context = transfer_context

        # call_id is the AudioSocket UUID -- the same value ARI passed as the
        # External Media `data` field, which is what correlated this connection
        # to its channel. A direct call has no UUID, so fall back to the peer
        # address, which is at least unique per connection.
        self.call_id = str(io.call_id) if io.call_id else f"direct-{addr[0]}:{addr[1]}"
        self.caller_id = ari_call.caller_id if ari_call else "unknown"

        # The underlying connection already has an asyncio.Event for this; reuse
        # it rather than duplicating state that could disagree.
        self.ended = io.hangup_event

    # -- the CallSession contract ------------------------------------------
    async def read_audio(self) -> bytes | None:
        """Next 320-byte frame of caller audio, or None once when the call ends.

        The None sentinel is pushed onto `incoming` by the read thread's
        _signal_end(), so a hangup wakes this await immediately instead of
        leaving the engine blocked forever.
        """
        return await self._io.incoming.get()

    async def write_audio(self, pcm: bytes) -> None:
        """Hand one frame to the write thread. Blocks (async) to pace playback."""
        await asyncio.get_event_loop().run_in_executor(
            None, self._io.queue_output, pcm
        )

    async def transfer(self, destination: str) -> bool:
        """Send the caller back into the dialplan at [transfer] <destination>.

        `destination` (the department the LLM chose) becomes the dialplan
        EXTENSION, so extensions.conf decides who actually gets dialled -- and
        the DIALSTATUS "no one available" handling stays there too, where a
        telephony admin can change it without touching Python.

        Leaving Stasis drops the External Media leg, which ends this call's
        pipeline cleanly; our StasisEnd handler destroys the bridge.
        """
        if not self.can_transfer:
            logger.warning(
                f"transfer to '{destination}' requested on {self.call_id}, which has "
                "no ARI channel (direct AudioSocket call) -- ignoring"
            )
            return False
        await self._controller.transfer(
            self._ari_call.channel_id,
            context=self._transfer_context,
            extension=destination,
        )
        return True

    async def hangup(self) -> None:
        """Close the audio path. Safe to call more than once.

        DELIBERATELY audio-only: we do NOT issue an ARI hangup on the caller's
        channel. This runs in the engine's `finally`, which also executes after
        a successful transfer -- and at that point the channel belongs to the
        dialplan and may be mid-conversation with a human. Destroying it here
        would cut off the very call we just transferred.

        Cleanup of the bridge and the External Media channel is handled by the
        ARI StasisEnd handler, which fires whichever way the call ends.
        """
        self._io.stop()

    @property
    def can_transfer(self) -> bool:
        return self._controller is not None and self._ari_call is not None

    # -- Asterisk-specific extras used for logging / diagnostics -----------
    @property
    def end_reason(self) -> str:
        return self._io.end_reason

    @property
    def io(self) -> AudioSocketConnection:
        """Escape hatch for frame counters and the like. Engine code should not
        reach through this -- it is vendor-specific by definition."""
        return self._io

    def stats(self) -> str:
        """Frame counters for the closing log line -- the fastest diagnostic we
        have. in=0 means we never heard the caller; real=0 means the agent
        never spoke."""
        return (
            f"in={self._io.frames_in} out={self._io.frames_out} "
            f"(real={self._io.frames_out_real})"
        )


class AsteriskTransport(BaseTransport):
    """Accepts AudioSocket connections and (optionally) runs ARI call control.

    Owns three things:
      1. the listening TCP socket + a blocking accept() thread,
      2. the ARI controller (Stasis app, mixing bridge, External Media),
      3. the correlation step that joins 1 and 2 into one CallSession.

    ARI is OPTIONAL. With no ARI password the transport still serves direct
    AudioSocket calls -- conversation works, transfer does not. That is the
    documented degraded mode, and it is silent, so it is the first thing to
    check when "transfer stopped working".
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8090,
        ari_base_url: str = "http://localhost:8088",
        ari_app: str = "voiceagent",
        ari_user: str = "voiceagent",
        ari_password: str | None = None,
        transfer_context: str = "transfer",
        media_host: str = "127.0.0.1",
        backlog: int = 8,
    ):
        self._host = host
        self._port = port
        self._ari_base_url = ari_base_url
        self._ari_app = ari_app
        self._ari_user = ari_user
        self._ari_password = ari_password
        self._transfer_context = transfer_context
        self._media_host = media_host
        self._backlog = backlog

        self._lsock: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._controller: AriController | None = None
        self._ari_task: asyncio.Task | None = None
        self._running = False

        # Fully-prepared sessions, waiting to be handed to listen(). Filled by
        # per-call intake tasks (see _intake) so that correlating call N never
        # delays call N+1.
        self._ready: asyncio.Queue = asyncio.Queue()

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        loop = asyncio.get_event_loop()
        self._running = True

        self._lsock = self._make_listen_socket()
        self._lsock.listen(self._backlog)
        self._lsock.setblocking(True)

        def accept_loop():
            """Blocking accept() on its own thread.

            Deliberately NOT asyncio.start_server: we need the listening socket
            configured before bind (see _make_listen_socket) and we hand the raw
            socket to threads anyway. Each accepted connection is dispatched
            back onto the event loop with run_coroutine_threadsafe.
            """
            while self._running:
                try:
                    conn, addr = self._lsock.accept()
                except OSError:
                    break  # socket closed by stop()
                asyncio.run_coroutine_threadsafe(self._intake(conn, addr), loop)

        self._accept_thread = threading.Thread(target=accept_loop, daemon=True)
        self._accept_thread.start()
        logger.info(f"AudioSocket server listening on {self._host}:{self._port}")

        if self._ari_password:
            self._controller = AriController(
                base_url=self._ari_base_url,
                app=self._ari_app,
                user=self._ari_user,
                password=self._ari_password,
                media_host=self._media_host,
                media_port=self._port,
            )
            self._ari_task = asyncio.create_task(self._controller.run())
            logger.info("Call 6000 (direct) or 6001 (via ARI/Stasis) to talk to it.")
        else:
            logger.info("Call extension 6000 to talk to it. (ARI not configured.)")

    async def stop(self) -> None:
        self._running = False
        if self._lsock:
            try:
                self._lsock.close()  # unblocks the accept thread
            except OSError:
                pass
            self._lsock = None
        if self._ari_task:
            self._ari_task.cancel()
            try:
                await self._ari_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._ari_task = None

    def _make_listen_socket(self) -> socket.socket:
        """Create the listening socket with a large receive buffer set BEFORE bind.

        This is the real fix for Asterisk dropping calls with "Resource
        temporarily unavailable". TCP negotiates its window-scaling factor
        during the handshake, from the LISTENING socket's buffer size. Enlarging
        the buffer after accept() grows the memory but leaves the advertised
        window clamped small, so Asterisk still overflows. Setting SO_RCVBUF
        here, before bind/listen, lets every accepted connection negotiate a
        large window from its very first packet.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # NOTE: do NOT set SO_REUSEADDR on Windows -- it raises WinError 10013
        # on bind. asyncio itself skips it on Windows for the same reason.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RECV_BUFFER_BYTES)
        sock.bind((self._host, self._port))
        return sock

    # -- per-call intake ---------------------------------------------------
    async def _intake(self, conn: socket.socket, addr):
        """Turn one accepted socket into one ready CallSession.

        Runs as its own task per call, which is what keeps concurrent calls
        independent: the up-to-2 s correlation wait below happens in parallel
        for every call rather than serialising them behind listen().
        """
        logger.info(f"--- Call connected from {addr} ---")
        conn.setblocking(True)

        io = AudioSocketConnection(conn, asyncio.get_event_loop())
        io.start()  # start the I/O threads IMMEDIATELY -- drains from frame 0

        ari_call = await self._correlate(io)
        session = AsteriskCallSession(
            io=io,
            addr=addr,
            controller=self._controller,
            ari_call=ari_call,
            transfer_context=self._transfer_context,
        )
        await self._ready.put(session)

    async def _correlate(self, io: AudioSocketConnection) -> AriCall | None:
        """Join this audio connection to its ARI caller channel, if it has one.

        The mechanism: ARI generated a UUID and passed it to externalMedia as
        the `data` field; Asterisk's FIRST AudioSocket message on the new
        connection is that same UUID (type 0x01). The read thread parses it and
        sets `uuid_ready`, so we wait briefly for it and then look the UUID up
        in the controller's registry. The registry entry is written BEFORE the
        media channel is created, so this lookup cannot lose a race.

        Returns None for a direct (non-ARI) call -- not an error.
        """
        if self._controller is None:
            return None
        try:
            await asyncio.wait_for(
                io.uuid_ready.wait(), timeout=UUID_CORRELATION_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            logger.debug(
                f"no UUID within {UUID_CORRELATION_TIMEOUT_S}s; treating as a non-ARI call"
            )
            return None

        ari_call = self._controller.registry.get(str(io.call_id))
        if ari_call:
            logger.info(f"call correlated to ARI channel {ari_call.channel_id}")
        return ari_call

    # -- the BaseTransport contract ----------------------------------------
    async def listen(self) -> AsyncIterator[CallSession]:
        """Yield each incoming call once its session is fully prepared."""
        while self._running:
            session = await self._ready.get()
            yield session

    async def reject(self, call: CallSession) -> None:
        """Refuse a call. Closes the audio path immediately.

        Not used by the Asterisk path today: capacity and busy handling belong
        in the dialplan, where Asterisk can play a message without our help.
        Provided because the contract requires it, and because cloud transports
        with no dialplan must do it app-side.
        """
        logger.warning(f"rejecting call {call.call_id}")
        await call.hangup()


def transport_from_env() -> AsteriskTransport:
    """Build the transport from environment variables -- the same ones bot.py
    used before this refactor, so nothing about configuration changed yet.

    Phase 4 replaces this with a config.yaml loader + factory. It is a separate
    function so that swap touches one call site.
    """
    return AsteriskTransport(
        host=os.getenv("AUDIOSOCKET_HOST", "0.0.0.0"),
        port=int(os.getenv("AUDIOSOCKET_PORT", "8090")),
        ari_base_url=os.getenv("ARI_BASE_URL", "http://localhost:8088"),
        ari_app=os.getenv("ARI_APP", "voiceagent"),
        ari_user=os.getenv("ARI_USER", "voiceagent"),
        ari_password=os.getenv("ARI_PASSWORD"),
        transfer_context=os.getenv("TRANSFER_CONTEXT", "transfer"),
    )
