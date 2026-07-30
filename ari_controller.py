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
import asyncio
import base64
import json
import uuid
from dataclasses import dataclass

import aiohttp
from loguru import logger

# How long to wait for our external-media channel to enter the Stasis app before
# giving up and trying to bridge it anyway. It normally arrives in a few
# milliseconds; this is a safety net, not a delay we expect to spend.
EM_STASIS_TIMEOUT_S = 2.0


@dataclass
class AriCall:
    """Everything we need to control (and later transfer) one live call."""

    channel_id: str  # the CALLER's channel -- what a transfer acts on
    bridge_id: str
    em_id: str  # the external-media channel id
    audiosocket_uuid: str
    # Caller's number as Asterisk reported it, or "unknown". Surfaced as
    # CallSession.caller_id. Treat as untrusted -- on most trunks it is
    # caller-supplied.
    caller_id: str = "unknown"


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

        # em-channel-id -> Event, set when THAT channel's own StasisStart
        # arrives. externalMedia returns before the channel has entered Stasis,
        # so we must wait for this before adding it to a bridge (see B-010).
        self._em_ready: dict[str, asyncio.Event] = {}
        # Caller channels whose StasisEnd arrived while their setup was still in
        # flight -- i.e. the caller hung up mid-setup. Their setup task cleans up
        # when it finishes, because _on_stasis_end found nothing to clean yet.
        self._ended_early: set[str] = set()
        # Strong refs to in-flight handler tasks; without these CPython may
        # garbage-collect a running task.
        self._tasks: set[asyncio.Task] = set()

    # -- REST helpers ------------------------------------------------------
    async def _req(self, method, path, **params):
        """Returns the parsed JSON body, or True when a call succeeded with no
        body, or **None on failure**.

        The True matters: many ARI commands answer 204 No Content (addChannel,
        answer, hangup, continue). Returning None for those too would make
        "worked fine" indistinguishable from "failed", and any caller that
        checked the result would treat every success as an error.
        """
        url = f"{self._base}/ari{path}"
        async with self._session.request(method, url, params=params) as r:
            body = await r.text()
            if r.status >= 400:
                logger.error(f"ARI {method} {path} -> {r.status}: {body}")
                return None
            return json.loads(body) if body else True

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

        caller = chan.get("caller", {}).get("number") or "?"
        logger.info(f"ARI: call in {cid} ({chan.get('name')}) from {caller}")

        au = str(uuid.uuid4())
        em_id = "em-" + uuid.uuid4().hex
        self._em_ids.add(em_id)
        # Create the gate BEFORE the channel exists, so its StasisStart can never
        # arrive before we are ready to notice it.
        self._em_ready[em_id] = asyncio.Event()

        await self.answer(cid)
        bridge = await self._create_bridge()
        if not bridge:
            self._forget_em(em_id)
            return
        bid = bridge["id"]
        await self._add(bid, cid)

        self.registry[au] = AriCall(cid, bid, em_id, au, caller)

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
            self._forget_em(em_id)
            return

        # B-010: externalMedia returns as soon as the channel is CREATED, but
        # addChannel requires it to have ENTERED Stasis -- which happens a moment
        # later and is announced by its own StasisStart. Adding it immediately
        # races that, and losing the race means 422 and a call with no audio path
        # at all. So wait for the event rather than hoping.
        if not await self._wait_for_em_stasis(em_id):
            logger.warning(
                f"ARI: media channel {em_id} did not enter Stasis within "
                f"{EM_STASIS_TIMEOUT_S}s -- adding it anyway as a last resort"
            )

        # The caller can hang up while we wait, in which case _on_stasis_end has
        # already torn this call down. Bridging into a destroyed bridge would
        # just log a confusing failure, so stop here.
        if au not in self.registry:
            logger.info(f"ARI: {cid} went away during setup; abandoning bridging")
            self._release_gate(em_id)
            return

        # Check the result. This used to be ignored while the next line logged
        # success unconditionally, so a failure here looked like a healthy call.
        if await self._add(bid, em_id) is None:
            logger.error(
                f"ARI: could not add media channel {em_id} to bridge {bid} -- "
                f"call {cid} has NO audio path. Tearing it down instead of "
                "leaving the caller in silence."
            )
            await self.hangup(em_id)
            await self._destroy_bridge(bid)
            self.registry.pop(au, None)
            self._forget_em(em_id)
            return

        logger.info(f"ARI: {cid} bridged into the AI pipeline (uuid={au})")

        # The caller may have hung up while we were setting all this up. In that
        # case _on_stasis_end ran before the registry entry existed and found
        # nothing to clean, so it is our job now.
        if cid in self._ended_early:
            self._ended_early.discard(cid)
            logger.info(f"ARI: {cid} hung up during setup; cleaning up immediately")
            await self._teardown(self.registry[au])

    async def _wait_for_em_stasis(self, em_id: str) -> bool:
        """Wait for our external-media channel's own StasisStart. True if it came.

        NOTE: this only works because StasisStart handling is dispatched as a
        task (see run()). If handlers ran inline on the WebSocket read loop, the
        event we are waiting for could never be read and this would deadlock for
        the full timeout, every call.
        """
        gate = self._em_ready.get(em_id)
        if gate is None:
            return False
        try:
            await asyncio.wait_for(gate.wait(), timeout=EM_STASIS_TIMEOUT_S)
            return True
        except asyncio.TimeoutError:
            return False

    def _forget_em(self, em_id: str):
        """Forget a media channel that was NEVER CREATED, so no events can come.

        Only safe on the failure paths before/at externalMedia. Once the channel
        exists, its id must stay in _em_ids until its StasisEnd -- see
        _release_gate.
        """
        self._em_ids.discard(em_id)
        self._em_ready.pop(em_id, None)

    def _release_gate(self, em_id: str):
        """Drop the StasisStart gate, but KEEP the id in _em_ids.

        Deliberate: a media channel's StasisStart may still be in flight when we
        tear down (the caller can hang up mid-setup). If we forgot the id here,
        that pending event would no longer be recognised as ours and would be
        handled as a NEW INCOMING CALL -- answering our own media channel and
        building a phantom bridge for it. The id is retired in _on_stasis_end,
        which is that channel's genuinely final event.
        """
        self._em_ready.pop(em_id, None)

    async def _teardown(self, call: AriCall):
        logger.info(f"ARI: call {call.channel_id} ended; cleaning up bridge {call.bridge_id}")
        await self.hangup(call.em_id)
        await self._destroy_bridge(call.bridge_id)
        self.registry.pop(call.audiosocket_uuid, None)
        self._release_gate(call.em_id)

    async def _on_stasis_end(self, event):
        cid = event["channel"]["id"]
        # Our own media channels end too. Nothing to clean up, but THIS is where
        # we retire the id: it is the channel's final event, so no later event
        # can be misread as an incoming call (see _release_gate).
        if cid in self._em_ids:
            self._em_ids.discard(cid)
            self._em_ready.pop(cid, None)
            return
        call = next((c for c in self.registry.values() if c.channel_id == cid), None)
        if not call:
            # Either not ours, or the caller hung up before setup finished. Note
            # it so the setup task can clean up when it completes.
            self._ended_early.add(cid)
            return
        await self._teardown(call)

    # -- event dispatch ----------------------------------------------------
    def _spawn(self, coro):
        """Run a handler as a task, so the WebSocket read loop keeps draining.

        This is load-bearing, not tidiness: call setup has to WAIT for another
        ARI event (the media channel's StasisStart). Handled inline, that event
        could never be read, because the read loop would be blocked inside the
        handler waiting for it -- a guaranteed deadlock.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _dispatch(self, event):
        etype = event.get("type")
        cid = event.get("channel", {}).get("id")

        if etype == "StasisStart":
            # Our OWN external-media channel enters Stasis too (B-009), and must
            # never be mistaken for an incoming call. Handle it inline: all it
            # does is open the gate that a setup task is waiting on, so there is
            # nothing to await and no reason to delay it by a scheduling hop.
            if cid in self._em_ids:
                gate = self._em_ready.get(cid)
                if gate:
                    gate.set()
                return
            self._spawn(self._on_stasis_start(event))
        elif etype == "StasisEnd":
            self._spawn(self._on_stasis_end(event))

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
                        self._dispatch(json.loads(msg.data))
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
            # Handler tasks outlive the read loop; don't leave them dangling.
            for task in list(self._tasks):
                task.cancel()
            if self._session:
                await self._session.close()
