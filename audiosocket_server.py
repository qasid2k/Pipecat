"""
Phase 1: A bare AudioSocket TCP server.

Asterisk's AudioSocket() dialplan app opens a TCP connection to THIS program and
streams the caller's microphone audio to us as raw PCM. Anything we write back on
the same socket is played to the caller. No AI yet -- we just watch the bytes
arrive and (as a proof it works) echo them straight back so the caller hears
themselves.

AudioSocket wire format
-----------------------
Every message is:  [ 1 byte: TYPE ][ 2 bytes: LENGTH ][ LENGTH bytes: PAYLOAD ]
  * The 3-byte header's LENGTH is BIG-ENDIAN (network byte order).
  * For audio, the PAYLOAD is 16-bit signed PCM, 8 kHz, mono (little-endian
    samples). One 20 ms frame = 160 samples * 2 bytes = 320 bytes.

Run:  python audiosocket_server.py   (then call extension 6000)
"""

import asyncio
import struct
import uuid
import logging

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HOST = "0.0.0.0"   # 0.0.0.0 = listen on ALL network interfaces, so the Asterisk
                   # server on the LAN can reach us (not just localhost).
PORT = 8090        # Must match the port in the dialplan's AudioSocket() line.

# AudioSocket message TYPES (the first byte of every header)
TYPE_HANGUP = 0x00   # Asterisk -> us: the call ended. (We can also send this to hang up.)
TYPE_UUID   = 0x01   # Asterisk -> us, ONCE at the start: 16-byte unique call id.
TYPE_DTMF   = 0x03   # Asterisk -> us: the caller pressed a phone key.
TYPE_AUDIO  = 0x10   # Both directions: a chunk of raw PCM audio.
TYPE_ERROR  = 0xff   # Asterisk -> us: something went wrong (1-byte error code).

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("audiosocket")


def build_message(msg_type: int, payload: bytes = b"") -> bytes:
    """Wrap a payload in the 3-byte AudioSocket header.

    struct.pack(">BH", ...) means: big-endian (>), one unsigned byte (B) for the
    type, then one unsigned 16-bit int (H) for the length. That's our 3-byte
    header. We then append the raw payload.
    """
    header = struct.pack(">BH", msg_type, len(payload))
    return header + payload


async def read_message(reader: asyncio.StreamReader):
    """Read exactly ONE full AudioSocket message from the socket.

    We must read the 3-byte header first to learn how many payload bytes follow,
    then read exactly that many. readexactly() blocks until it has all the bytes
    (or raises if the connection dies). Returns (type, payload), or None if the
    peer closed the connection.
    """
    try:
        header = await reader.readexactly(3)
    except asyncio.IncompleteReadError:
        return None  # Asterisk closed the socket

    msg_type, length = struct.unpack(">BH", header)  # unpack the same way we packed

    payload = b""
    if length:
        try:
            payload = await reader.readexactly(length)
        except asyncio.IncompleteReadError:
            return None
    return msg_type, payload


async def handle_call(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Called once per incoming connection (i.e. once per phone call).

    Asterisk is the CLIENT here; our server accepts the connection and this
    coroutine runs for the life of that one call.
    """
    peer = writer.get_extra_info("peername")
    log.info(f"NEW CONNECTION from {peer}  <-- Asterisk connected to us")

    audio_frames = 0
    audio_bytes = 0
    try:
        while True:
            msg = await read_message(reader)
            if msg is None:
                log.info("Socket closed by Asterisk")
                break
            msg_type, payload = msg

            if msg_type == TYPE_UUID:
                # 16 raw bytes -> a readable UUID string. This id matches the one
                # we put in the dialplan, so later we can tie audio to a call.
                call_id = uuid.UUID(bytes=payload)
                log.info(f"CALL ID (UUID): {call_id}")

            elif msg_type == TYPE_AUDIO:
                audio_frames += 1
                audio_bytes += len(payload)
                # Don't spam the log: show the first few frames, then 1 per second.
                if audio_frames <= 3 or audio_frames % 50 == 0:
                    log.info(
                        f"AUDIO frame #{audio_frames}: {len(payload)} bytes of PCM"
                    )
                # --- ECHO: send the caller's audio right back to them ---
                # This proves both directions work. Comment these two lines out
                # if you'd rather just observe silently.
                writer.write(build_message(TYPE_AUDIO, payload))
                await writer.drain()

            elif msg_type == TYPE_DTMF:
                digit = chr(payload[0]) if payload else "?"
                log.info(f"DTMF key pressed: {digit}")

            elif msg_type == TYPE_HANGUP:
                log.info("HANGUP signal received")
                break

            elif msg_type == TYPE_ERROR:
                code = payload[0] if payload else -1
                log.info(f"ERROR from Asterisk (code {code})")

            else:
                log.info(f"Unknown message type 0x{msg_type:02x} ({len(payload)} bytes)")

    except ConnectionResetError:
        log.info("Connection reset by peer")
    finally:
        seconds = audio_bytes / 16000  # 8000 samples/s * 2 bytes = 16000 bytes/s
        log.info(
            f"CALL ENDED. Got {audio_frames} audio frames "
            f"({audio_bytes} bytes ~ {seconds:.1f}s of audio)"
        )
        writer.close()


async def main():
    server = await asyncio.start_server(handle_call, HOST, PORT)
    log.info(f"AudioSocket server listening on {HOST}:{PORT}")
    log.info("Waiting for Asterisk... (call extension 6000 to connect)")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down.")
