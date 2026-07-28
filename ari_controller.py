"""
Phase 3: ARI controller -- gives the voice agent call control.

Runs INSIDE bot.py, alongside the AudioSocket server. When a call enters the
Stasis app (dialplan: `Stasis(voiceagent)`), this controller:

  1. answers the caller,
  2. puts them in a mixing bridge,
  3. attaches an "External Media" channel that streams the audio over AudioSocket
     to bot.py's own server (127.0.0.1:8090) -- so the normal AI pipeline runs,
  4. remembers which caller channel goes with which AudioSocket UUID, so a
     `transfer_to_department` tool can act on the right call.

This is the exact logic proven end-to-end by ari_media_test.py, refactored into
a reusable class and pointed at the real pipeline instead of an echo server.

If ARI isn't configured (no ARI_PASSWORD) or unreachable, bot.py still runs the
direct-AudioSocket path (extension 6000) -- call control is simply disabled.
"""
import base64
import json
import uuid
from dataclasses import dataclass

import aiohttp
from loguru import logger


@dataclass
class AriCall:
    """Everything we need to control (and later transfer) one live call."""

    channel_id: str  # the CALLER's channel -- what a transfer acts on
    bridge_id: str
    em_id: str  # the external-media channel id
    audiosocket_uuid: str


class AriController:
    def __init__(self, base_url, app, user, password, media_host, media_port):
        self._base = base_url.rstrip("/")
        self._app = app
        self._user = user
        self._password = password
        self._media = f"{media_host}:{media_port}"
        self._session: aiohttp.ClientSession | None = None

        # audiosocket-uuid -> AriCall. bot.py reads this to correlate an
        # incoming AudioSocket connection with its ARI call.
        self.registry: dict[str, AriCall] = {}
        self._em_ids: set[str] = set()  # our own media channels, to skip

    # -- REST helpers ------------------------------------------------------
    async def _req(self, method, path, **params):
        url = f"{self._base}/ari{path}"
        async with self._session.request(method, url, params=params) as r:
            body = await r.text()
            if r.status >= 400:
                logger.error(f"ARI {method} {path} -> {r.status}: {body}")
                return None
            return json.loads(body) if body else None

    async def answer(self, cid):
        return await self._req("POST", f"/channels/{cid}/answer")

    async def hangup(self, cid):
        return await self._req("DELETE", f"/channels/{cid}")

    async def continue_in_dialplan(self, cid, context, extension, priority=1):
        """Send a channel back to the dialplan -- used to hand off / transfer."""
        return await self._req(
            "POST",
            f"/channels/{cid}/continue",
            context=context,
            extension=extension,
            priority=priority,
        )

    async def transfer(self, channel_id, context="transfer", extension="human", priority=1):
        """Hand the caller off to a human.

        We send the caller channel back to the dialplan at
        `[context] extension,priority`, where YOU decide (in extensions.conf)
        who "human" dials -- e.g. Dial(PJSIP/102). Leaving Stasis drops the
        external-media leg, which ends this call's AI pipeline cleanly (our
        StasisEnd handler tears down the bridge).
        """
        logger.info(f"ARI: transferring {channel_id} -> {context},{extension},{priority}")
        return await self.continue_in_dialplan(channel_id, context, extension, priority)

    async def _create_bridge(self):
        return await self._req("POST", "/bridges", type="mixing")

    async def _add(self, bridge_id, channel_id):
        return await self._req("POST", f"/bridges/{bridge_id}/addChannel", channel=channel_id)

    async def _destroy_bridge(self, bridge_id):
        return await self._req("DELETE", f"/bridges/{bridge_id}")

    async def _external_media(self, **params):
        return await self._req("POST", "/channels/externalMedia", **params)

    # -- event handlers ----------------------------------------------------
    async def _on_stasis_start(self, event):
        chan = event["channel"]
        cid = chan["id"]

        # Our own external-media channel also enters Stasis -- ignore it.
        if cid in self._em_ids:
            return

        caller = chan.get("caller", {}).get("number") or "?"
        logger.info(f"ARI: call in {cid} ({chan.get('name')}) from {caller}")

        au = str(uuid.uuid4())
        em_id = "em-" + uuid.uuid4().hex
        self._em_ids.add(em_id)

        await self.answer(cid)
        bridge = await self._create_bridge()
        if not bridge:
            return
        bid = bridge["id"]
        await self._add(bid, cid)

        self.registry[au] = AriCall(cid, bid, em_id, au)

        em = await self._external_media(
            app=self._app,
            channelId=em_id,
            external_host=self._media,
            format="slin",  # 8kHz 16-bit signed linear = our pipeline format
            encapsulation="audiosocket",
            transport="tcp",
            connection_type="client",  # Asterisk dials OUT to our AudioSocket server
            data=au,
        )
        if not em:
            logger.error("ARI: external media creation failed -- call cannot get audio")
            self.registry.pop(au, None)
            return
        await self._add(bid, em_id)
        logger.info(f"ARI: {cid} bridged into the AI pipeline (uuid={au})")

    async def _on_stasis_end(self, event):
        cid = event["channel"]["id"]
        call = next((c for c in self.registry.values() if c.channel_id == cid), None)
        if not call:
            return
        logger.info(f"ARI: call {cid} ended; cleaning up bridge {call.bridge_id}")
        await self.hangup(call.em_id)
        await self._destroy_bridge(call.bridge_id)
        self.registry.pop(call.audiosocket_uuid, None)
        self._em_ids.discard(call.em_id)

    # -- main loop ---------------------------------------------------------
    async def run(self):
        """Connect to ARI and process events until the connection drops."""
        token = base64.b64encode(f"{self._user}:{self._password}".encode()).decode()
        self._session = aiohttp.ClientSession(headers={"Authorization": f"Basic {token}"})

        ws_scheme = "wss" if self._base.startswith("https") else "ws"
        host = self._base.split("://", 1)[1]
        ws_url = (
            f"{ws_scheme}://{host}/ari/events"
            f"?app={self._app}&api_key={self._user}:{self._password}"
        )
        try:
            async with self._session.ws_connect(ws_url) as ws:
                logger.info(f"ARI call control ENABLED (app '{self._app}')")
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        event = json.loads(msg.data)
                        etype = event.get("type")
                        if etype == "StasisStart":
                            await self._on_stasis_start(event)
                        elif etype == "StasisEnd":
                            await self._on_stasis_end(event)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        logger.warning("ARI WebSocket closed.")
                        break
        except aiohttp.ClientError as e:
            logger.error(
                f"ARI controller could not connect: {e!r} "
                "-- call control disabled; direct AudioSocket (6000) still works."
            )
        except Exception as e:  # noqa: BLE001
            logger.exception(f"ARI controller crashed: {e}")
        finally:
            if self._session:
                await self._session.close()
