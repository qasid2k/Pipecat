"""
Phase 3, Step 3 verification: audio routed THROUGH Stasis via External Media.

This proves the new call topology end-to-end WITHOUT the AI pipeline, so we
verify one thing at a time. A call to 6001 enters our Stasis app; our code
answers it, puts it in a bridge, and attaches an "External Media" channel that
streams the audio to a tiny AudioSocket server in THIS script. That server just
echoes the audio back -- so if you HEAR YOURSELF, the entire new path works:

    caller  <->  bridge  <->  external-media  <->  AudioSocket (echo)

It also confirms how a call is correlated to its audio connection: we pass a
UUID as the External Media `data` field and check the AudioSocket connection
reports that same UUID. That correlation is what a "transfer" tool will rely on.

NON-disruptive: uses extension 6001 and AudioSocket port 8092, so the real voice
bot (6000, port 8090) is untouched -- you can leave bot.py running.

Dialplan on the VM (same as the Step-2 test -- reuse it):
    exten => 6001,1,Stasis(voiceagent)
     same => n,Hangup()

Run:  python3 ari_media_test.py   then dial 6001 and talk.
"""
import asyncio
import base64
import json
import os
import struct
import uuid

import aiohttp
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

ARI_BASE = os.getenv("ARI_BASE_URL", "http://localhost:8088")
ARI_APP = os.getenv("ARI_APP", "voiceagent")
ARI_USER = os.getenv("ARI_USER", "voiceagent")
ARI_PASSWORD = os.getenv("ARI_PASSWORD", "")

# Where our echo AudioSocket server listens. Deliberately NOT 8090, so this can
# run alongside the real bot without a port clash.
MEDIA_HOST = "127.0.0.1"
MEDIA_PORT = 8092

# --- AudioSocket protocol (subset) -----------------------------------------
TYPE_HANGUP = 0x00
TYPE_UUID = 0x01
TYPE_AUDIO = 0x10


def build_message(msg_type: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BH", msg_type, len(payload)) + payload


# audiosocket-uuid -> caller channel id (set by the ARI side, read by the media
# server so it can confirm the correlation).
pending: dict[str, str] = {}
# caller channel id -> teardown info
sessions: dict[str, dict] = {}
# channel ids we created as external media -- skip their StasisStart.
em_channels: set[str] = set()


# ===========================================================================
#  ARI REST wrapper
# ===========================================================================
class Ari:
    def __init__(self, session: aiohttp.ClientSession):
        self._s = session

    async def _req(self, method: str, path: str, **params):
        url = f"{ARI_BASE}/ari{path}"
        async with self._s.request(method, url, params=params) as r:
            body = await r.text()
            if r.status >= 400:
                logger.error(f"{method} {path} -> {r.status}: {body}")
                return None
            return json.loads(body) if body else None

    async def answer(self, cid):
        return await self._req("POST", f"/channels/{cid}/answer")

    async def hangup(self, cid):
        return await self._req("DELETE", f"/channels/{cid}")

    async def create_bridge(self):
        return await self._req("POST", "/bridges", type="mixing")

    async def add_channel(self, bridge_id, channel_id):
        return await self._req("POST", f"/bridges/{bridge_id}/addChannel", channel=channel_id)

    async def destroy_bridge(self, bridge_id):
        return await self._req("DELETE", f"/bridges/{bridge_id}")

    async def external_media(self, **params):
        return await self._req("POST", "/channels/externalMedia", **params)


# ===========================================================================
#  AudioSocket echo server -- proves audio flows both ways through the bridge
# ===========================================================================
async def handle_audiosocket(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    got_uuid = None
    frames = 0
    try:
        while True:
            try:
                hdr = await reader.readexactly(3)
            except asyncio.IncompleteReadError:
                break
            msg_type = hdr[0]
            length = (hdr[1] << 8) | hdr[2]
            payload = await reader.readexactly(length) if length else b""

            if msg_type == TYPE_UUID:
                got_uuid = str(uuid.UUID(bytes=payload))
                match = pending.get(got_uuid)
                logger.info(
                    f"AudioSocket connected. UUID={got_uuid} -> "
                    f"{'MATCHES caller ' + match if match else 'NO MATCH (correlation broken!)'}"
                )
            elif msg_type == TYPE_AUDIO:
                frames += 1
                # Echo it straight back: caller hears themselves.
                writer.write(build_message(TYPE_AUDIO, payload))
                await writer.drain()
                if frames % 150 == 0:
                    logger.info(f"echoed {frames} frames ({frames * 0.02:.0f}s of audio)")
            elif msg_type == TYPE_HANGUP:
                break
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        logger.info(f"AudioSocket closed (uuid={got_uuid}, {frames} frames echoed)")
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
#  ARI event handling
# ===========================================================================
async def on_stasis_start(ari: Ari, event: dict):
    chan = event["channel"]
    cid = chan["id"]

    # Our own external-media channel also enters Stasis -- ignore it.
    if cid in em_channels:
        logger.debug(f"(external media channel {cid} entered Stasis; ignoring)")
        return

    caller = chan.get("caller", {}).get("number") or "?"
    logger.info(f"CALLER IN: {cid} ({chan.get('name')}) from {caller}")

    audiosocket_uuid = str(uuid.uuid4())
    em_id = "em-" + uuid.uuid4().hex
    em_channels.add(em_id)

    await ari.answer(cid)

    bridge = await ari.create_bridge()
    if not bridge:
        logger.error("bridge creation failed")
        return
    bid = bridge["id"]
    await ari.add_channel(bid, cid)

    pending[audiosocket_uuid] = cid
    sessions[cid] = {"bridge": bid, "em": em_id, "uuid": audiosocket_uuid}

    logger.info(
        f"bridge {bid} made; creating external media -> {MEDIA_HOST}:{MEDIA_PORT} "
        f"(uuid={audiosocket_uuid})"
    )
    em = await ari.external_media(
        app=ARI_APP,
        channelId=em_id,
        external_host=f"{MEDIA_HOST}:{MEDIA_PORT}",
        format="slin",  # 8kHz 16-bit signed linear = our audio format
        encapsulation="audiosocket",
        transport="tcp",
        connection_type="client",  # Asterisk connects OUT to our server
        data=audiosocket_uuid,
    )
    if not em:
        logger.error("external media creation FAILED -- try format='slin8' or 'ulaw' next")
        return
    await ari.add_channel(bid, em_id)
    logger.info("external media in bridge -- SPEAK and you should hear an echo")


async def on_stasis_end(ari: Ari, event: dict):
    cid = event["channel"]["id"]
    sess = sessions.pop(cid, None)
    if not sess:
        return
    logger.info(f"CALLER GONE: {cid} -- tearing down bridge {sess['bridge']}")
    await ari.hangup(sess["em"])
    await ari.destroy_bridge(sess["bridge"])
    pending.pop(sess["uuid"], None)
    em_channels.discard(sess["em"])


async def main():
    if not ARI_PASSWORD:
        logger.error("ARI_PASSWORD is not set in .env -- add it and retry.")
        return

    token = base64.b64encode(f"{ARI_USER}:{ARI_PASSWORD}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    async with aiohttp.ClientSession(headers=headers) as session:
        ari = Ari(session)

        media_srv = await asyncio.start_server(handle_audiosocket, MEDIA_HOST, MEDIA_PORT)
        logger.info(f"AudioSocket echo server listening on {MEDIA_HOST}:{MEDIA_PORT}")

        ws_scheme = "wss" if ARI_BASE.startswith("https") else "ws"
        host = ARI_BASE.split("://", 1)[1]
        ws_url = f"{ws_scheme}://{host}/ari/events?app={ARI_APP}&api_key={ARI_USER}:{ARI_PASSWORD}"

        logger.info(f"Connecting to ARI at {ARI_BASE} as app '{ARI_APP}'...")
        try:
            async with session.ws_connect(ws_url) as ws:
                logger.info("ARI connected. Dial 6001 and talk. (Ctrl+C to stop.)")
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        event = json.loads(msg.data)
                        etype = event.get("type")
                        if etype == "StasisStart":
                            await on_stasis_start(ari, event)
                        elif etype == "StasisEnd":
                            await on_stasis_end(ari, event)
                        else:
                            logger.debug(f"event: {etype}")
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        logger.warning("ARI WebSocket closed.")
                        break
        except aiohttp.ClientError as e:
            logger.error(f"Could not connect to ARI: {e!r}")
        finally:
            media_srv.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
