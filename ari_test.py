"""
Phase 3, Step 2: minimal ARI proof-of-control.

This does NOT touch the working voice bot (bot.py) or extension 6000. Its only
job is to prove the "control plane" works: that we can log into Asterisk's ARI,
receive call events over its WebSocket, and command a live call -- answer it,
play a sound, hang it up. That control is exactly what the voice agent lacks
today (AudioSocket carries audio only, no way to say "transfer this call").

ARI has two halves, and this script uses both:
  * a WebSocket that PUSHES events to us   (a call arrived, a call ended, ...)
  * a REST API we call to ISSUE commands   (answer, play, hangup, later: transfer)

Set up on the VM (a SEPARATE extension so 6000 / the voice bot keeps working):
    ; /etc/asterisk/extensions.conf
    exten => 6001,1,Stasis(voiceagent)
     same => n,Hangup()
  then: asterisk -rx "dialplan reload"

Add to .env on the VM (password = your new ari.conf password):
    ARI_PASSWORD=<the strong password you just set>

Run:  python3 ari_test.py     then dial 6001 from your softphone.
You should hear "hello world", then the call hangs up -- all driven by ARI.
"""
import asyncio
import json
import os

import aiohttp
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

ARI_BASE = os.getenv("ARI_BASE_URL", "http://localhost:8088")
ARI_APP = os.getenv("ARI_APP", "voiceagent")
ARI_USER = os.getenv("ARI_USER", "voiceagent")
ARI_PASSWORD = os.getenv("ARI_PASSWORD", "")


class Ari:
    """Thin wrapper over the ARI REST API -- just the calls we need."""

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

    async def answer(self, channel_id: str):
        return await self._req("POST", f"/channels/{channel_id}/answer")

    async def play(self, channel_id: str, sound: str):
        # media "sound:hello-world" plays a built-in Asterisk prompt.
        return await self._req("POST", f"/channels/{channel_id}/play", media=f"sound:{sound}")

    async def hangup(self, channel_id: str):
        return await self._req("DELETE", f"/channels/{channel_id}")


async def handle_event(ari: Ari, event: dict):
    """Dispatch one ARI event."""
    etype = event.get("type")

    if etype == "StasisStart":
        # A call entered our Stasis app (someone dialed 6001).
        chan = event["channel"]
        cid = chan["id"]
        caller = chan.get("caller", {}).get("number") or "?"
        logger.info(f"CALL IN: channel {cid} from {caller} -- answering via ARI")
        await ari.answer(cid)
        logger.info("answered; playing 'hello-world'")
        await ari.play(cid, "hello-world")
        await asyncio.sleep(4)  # let the prompt finish
        logger.info("hanging up via ARI")
        await ari.hangup(cid)

    elif etype == "StasisEnd":
        logger.info(f"CALL ENDED: channel {event['channel']['id']}")

    elif etype == "PlaybackFinished":
        logger.debug("playback finished")

    else:
        logger.debug(f"event: {etype}")


async def main():
    if not ARI_PASSWORD:
        logger.error("ARI_PASSWORD is not set in .env -- add it and retry.")
        return

    auth = aiohttp.BasicAuth(ARI_USER, ARI_PASSWORD)  # for REST calls
    async with aiohttp.ClientSession(auth=auth) as session:
        ari = Ari(session)

        # WebSocket auth uses the api_key query param (user:pass). We build the
        # URL without ever logging the password.
        ws_scheme = "wss" if ARI_BASE.startswith("https") else "ws"
        host = ARI_BASE.split("://", 1)[1]
        ws_url = (
            f"{ws_scheme}://{host}/ari/events"
            f"?app={ARI_APP}&api_key={ARI_USER}:{ARI_PASSWORD}"
        )

        logger.info(f"Connecting to ARI at {ARI_BASE} as app '{ARI_APP}'...")
        try:
            async with session.ws_connect(ws_url) as ws:
                logger.info(f"ARI connected. Dial 6001 to test. (Ctrl+C to stop.)")
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await handle_event(ari, json.loads(msg.data))
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        logger.warning("ARI WebSocket closed.")
                        break
        except aiohttp.ClientError as e:
            logger.error(f"Could not connect to ARI: {e!r}")
            logger.error("Check ARI_BASE_URL, the ari.conf password, and that ARI is enabled.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
