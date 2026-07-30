# Architecture (as it exists today)

Status: **as-built, 2026-07-30, after Phase 2** (transport interface + Asterisk
adapter). Every detail here was read off the code, not remembered.
Related: [[decisions]], [[bugs]], [[runbook]], [[changelog]].

One Asterisk AI voice agent. **Single agent, single persona, single voice.**
No agent pool, no queueing, no call recording of audio (transcripts only).

---

## Layering (introduced in Phase 2)

```
        bot.py                 the conversation: persona, pipeline, tools
           |                   talks ONLY to CallSession
           v
   core/transport.py           CallSession + BaseTransport contracts
           ^                   knows nothing about any vendor
           |
  transports/asterisk.py       the Asterisk/FreePBX adapter
           |
   +-------+--------+
   |                |
ari_controller.py   audiosocket_transport.py
(ARI: control)      (AudioSocket: protocol + I/O threads)
```

The rule: **the arrows never skip the middle.** `bot.py` contains no socket, no
ARI call and no vendor concept; the adapter contains no Pipecat import.

## Components

| File | Role |
|---|---|
| `core/transport.py` | The vendor-neutral contracts: `CallSession` (one call) and `BaseTransport` (one vendor connection), plus the canonical audio-format constants. |
| `transports/asterisk.py` | **The Asterisk adapter.** `AsteriskTransport` owns the listening socket + accept thread, the ARI Stasis app, and the UUID correlation. `AsteriskCallSession` is one call, with `transfer()` done the ARI way. Also covers FreePBX. |
| `bot.py` | Entry point + the conversation: persona prompt, the Pipecat pipeline, the `transfer_to_department` tool, transcripts. Vendor-agnostic. |
| `audiosocket_transport.py` | Two halves: `AudioSocketConnection` (protocol + the two I/O threads, Asterisk-specific) and the Pipecat glue transports, which now read/write through a `CallSession` and so are vendor-neutral. Phase 3 splits them. |
| `ari_controller.py` | ARI/Stasis call control: answer, mixing bridge, External Media channel, UUID registry, transfer. |
| `audiosocket_server.py` | Earlier standalone echo/test server. Superseded by `bot.py`. |
| `ari_test.py`, `ari_media_test.py`, `nettest_*.py` | Throwaway probes used to prove ARI and the network path. Not part of the running system. |

Runs as **one process**: the asyncio event loop hosts the ARI controller and all
call pipelines; each call additionally owns two OS threads for socket I/O.

### The main loop
```python
transport = transport_from_env()          # Phase 4: from config.yaml
await transport.start()
async for session in transport.listen():
    asyncio.create_task(handle_call(session))   # a TASK, so calls run in parallel
```
`create_task`, not `await`: awaiting `handle_call` here would serialise calls and
destroy concurrency. Task references are held in `_active_calls` so CPython
cannot garbage-collect a running call.

---

## Two ways a call can arrive

This is easy to miss and matters a lot — only one of the two can transfer.

### Path A — direct AudioSocket (extension 6000)
Dialplan calls `AudioSocket(<uuid>,<host>:8090)` directly. Audio works, the
conversation works, but there is **no ARI channel**, so `ari_call` stays `None`
and `transfer_to_department` answers "transfer isn't available on this call".
This is the dev/laptop path.

### Path B — ARI / Stasis (extension 6001) — the real one
Dialplan calls `Stasis(voiceagent)`. This is the transfer-capable path and the
one the product depends on.

```
caller channel ──┐
                 ├── mixing bridge ── External Media channel ──TCP──> bot.py :8090
    (answered)  ─┘                    (slin, audiosocket, client)
```

`AriController._on_stasis_start` (`ari_controller.py:104`) does, in order:

1. skip the event if the channel is one of our own External Media channels
   (they enter Stasis too — tracked in `self._em_ids`);
2. `POST /channels/{id}/answer`;
3. `POST /bridges` (`type=mixing`), then add the caller channel;
4. register `registry[audiosocket_uuid] = AriCall(channel_id, bridge_id, em_id, uuid)`
   **before** creating the media channel, so the correlation can never lose a race;
5. `POST /channels/externalMedia` with `format=slin`,
   `encapsulation=audiosocket`, `transport=tcp`, `connection_type=client`,
   `external_host=127.0.0.1:8090`, `data=<the uuid>`;
6. add the External Media channel to the bridge.

`connection_type=client` means **Asterisk dials out to us**; we are the TCP
server. `media_host` is hardcoded `127.0.0.1` in `bot.py:506`, so the ARI path
only works when the bot runs **on the Asterisk VM**.

`_on_stasis_end` tears down: hang up the External Media channel, destroy the
bridge, drop the registry entry.

### UUID correlation (how an audio socket finds its channel)

The join between "a TCP connection appeared" and "this ARI caller channel":

1. ARI generates `au = uuid4()` and passes it as the External Media `data` field.
2. Asterisk opens the TCP connection and its **first AudioSocket message is
   type `0x01` (UUID)** carrying those 16 bytes.
3. The read thread parses it into `io.call_id` and sets `io.uuid_ready`
   (`audiosocket_transport.py:177-184`).
4. `handle_call` waits on `uuid_ready` for **2.0 s**, then looks up
   `ari_controller.registry[str(io.call_id)]` (`bot.py:394-402`).
5. Timeout ⇒ logged as a non-ARI call and handled as Path A.

---

## Audio bridge: AudioSocket

Protocol framing — `[1 byte TYPE][2 bytes LENGTH, big-endian][PAYLOAD]`:

| Type | Meaning |
|---|---|
| `0x00` | HANGUP — caller hung up |
| `0x01` | UUID — 16 raw bytes, first message on the connection |
| `0x03` | DTMF — one ASCII digit |
| `0x10` | AUDIO — PCM payload |
| `0xFF` | ERROR — one error code byte |

**Canonical audio format, end to end: 8 kHz / 16-bit signed / mono (slin).**
One 20 ms frame = **320 bytes**. Deepgram STT/TTS and Silero VAD all accept
8 kHz, so **there is no resampling anywhere** in the system.

### The CallSession contract, and how the adapter satisfies it

| Contract method | Asterisk implementation |
|---|---|
| `read_audio()` | `await io.incoming.get()` — the asyncio queue the read thread feeds. Returns `None` **once** when the call ends. |
| `write_audio(pcm)` | `run_in_executor(io.queue_output, pcm)` — the blocking bounded put that paces playback. |
| `transfer(dept)` | `POST /channels/{caller}/continue` into `[transfer] <dept>,1`. |
| `hangup()` | Closes the audio path **only** — see the warning below. |
| `can_transfer` | `True` only when this call has an ARI channel (Stasis, ext 6001). |
| `ended` / `end_reason` | The connection's existing `hangup_event` and reason string, reused rather than duplicated. |

> ⚠️ **`hangup()` must never issue an ARI hangup on the caller's channel.** It
> runs in the engine's `finally`, which also executes after a *successful*
> transfer — at which point the channel belongs to the dialplan and may be
> talking to a human. Hanging it up there would kill the call we just
> transferred. Bridge/media cleanup is `StasisEnd`'s job either way.

### Threading model (the trickiest part of the codebase)

Socket I/O deliberately does **not** run on the event loop. Asterisk's
AudioSocket writer is non-blocking with zero tolerance: if it cannot hand over a
frame the instant it is ready, it gives up and drops the call. The event loop
stalls (ONNX model load, GC, inference) were long enough to do exactly that.

```
   READ THREAD                          asyncio                     WRITE THREAD
 blocking recv(65536)                                          blocking sendall()
        │                                                              ▲
        │ parse messages                                               │ paced 20ms
        ▼                                                              │
 call_soon_threadsafe ──► asyncio.Queue "incoming" ──► pipeline ──► queue.Queue
                          maxsize soft 100                          "outgoing"
                          (drop OLDEST when full)                   maxsize 3
```

* **Read thread**: blocking `recv()` releases the GIL, so Asterisk is drained
  continuously regardless of what the pipeline is doing. Frames cross into
  asyncio via `loop.call_soon_threadsafe`. Backlog cap `MAX_QUEUED_FRAMES = 100`,
  dropping the *oldest* frame (stale audio is worthless).
* **Write thread**: every 20 ms it sends one queued agent frame **or a frame of
  silence**. Pacing is off `time.monotonic()`, not the queue timeout, and it
  resyncs if it falls more than 100 ms behind.
* **Lockstep / continuous send**: the socket must never go quiet. If we stop
  sending, Asterisk stops forwarding the caller's audio. Silence-when-idle is
  load-bearing, not a nicety.
* **Back-pressure = pacing**: `_outgoing` is bounded at 3 frames, and
  `write_audio_frame` does the blocking `put()` in an executor thread. The block
  *is* the mechanism that paces the agent's speech to real time and keeps
  Pipecat's bot-speaking timing honest.
* **Barge-in**: `flush_output()` drains `_outgoing` so an interrupted agent stops
  mid-sentence.
* Call end from either thread goes through `_signal_end(reason)`, which sets
  `hangup_event` and pushes a `None` sentinel into `incoming`. The reason string
  is logged, so a premature drop is self-diagnosing.

### Socket tuning that must not be lost
* `SO_RCVBUF = 1 MB` is set on the **listening** socket **before `bind()`**
  (`bot.py:454`). TCP window scaling is negotiated at handshake time from the
  listening socket, so setting it later per-connection does nothing. See [[bugs]].
* **No `SO_REUSEADDR` on Windows** — it raises WinError 10013 on bind.
* Windows only: `timeBeginPeriod(1)` for 1 ms timer resolution, otherwise a 20 ms
  sleep rounds to ~31 ms and audio paces ~1.6× too slow.

---

## Conversation pipeline (Pipecat 1.6.0)

Built per call in `build_pipeline` (`bot.py:263`):

```
transport.input()                 caller PCM in (8 kHz)
VADProcessor(SileroVADAnalyzer)   speech start/stop, barge-in
DeepgramSTTService                speech -> text        (sample_rate=8000)
TranscriptRecorder                appends each caller utterance to .jsonl
aggregators.user()                adds it to conversation history
GoogleLLMService                  gemini-flash-lite-latest
DeepgramTTSService                aura-2-helena-en       (sample_rate=8000)
transport.output()                PCM out to the write thread
aggregators.assistant()           records what the agent said
```

Services and settings (all **hardcoded today** — Phase 4 moves them to config):

* **STT** Deepgram, 8 kHz native.
* **LLM** Google Gemini `gemini-flash-lite-latest` (~700 ms measured; see
  [[decisions]] for why not `gemini-flash-latest`).
* **TTS** Deepgram Aura-2, voice `aura-2-helena-en`, 8 kHz.
* **Keys**: ONE `DEEPGRAM_API_KEY` serves both STT and TTS; ONE `GEMINI_API_KEY`.

### Turn-taking
Silero VAD **plus** an explicit stop strategy:
`SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)` wired through
`LLMUserAggregatorParams`. **Smart Turn v3 is disabled** — it is Pipecat's
default and loads a second ONNX model per utterance. VAD already reports
speech boundaries, so 0.6 s of silence = end of turn, no model needed.

### Per-call lifecycle
`handle_call` (`bot.py:370`) — one connection = one call = one independent
pipeline, each in its own `try/except`, so one call crashing cannot take down
the others. Concurrency is therefore natural: N calls = N pipelines + 2N threads.

* `PipelineWorker(idle_timeout_secs=30, app_resources=CallResources(...))` —
  30 s of total silence ends the call (vs the 5-minute default) so a dead test
  call does not hang around. `app_resources` is how the transfer tool reaches
  this call's ARI objects.
* `WorkerRunner(handle_sigint=False)` — the process owns signal handling.
* A `TTSSpeakFrame(GREETING)` is queued **before** the runner starts, so the
  caller is never greeted by silence.
* `watch_for_hangup()` waits on `io.hangup_event` and cancels the worker.
* `finally`: stop the socket, save the conversation, log duration + frame
  counters (`in`, `out`, `out_real`) and the recorded **CAUSE** of the ending.

### Transcripts (`recordings/`)
* `<stamp>-transcript.jsonl` — one line per finalized caller utterance, appended
  live, so a crashed call still leaves a usable record.
* `<stamp>-conversation.json` — the full two-sided conversation, written at end.
* `stamp = %Y%m%d-%H%M%S-` + 6 hex chars of a uuid4. The random tag exists
  because two calls in the same second overwrote each other. See [[bugs]].
* `_message_to_record` unwraps `LLMSpecificMessage` objects, which appear in the
  context once a tool has run and have no `.get()`. See [[bugs]].

---

## Transfer (works today — must never regress)

Transfer is a **call-control** operation, not a pipeline operation.

1. The LLM calls `transfer_to_department(department)`. Enum:
   **`sales | support | billing | human`**; anything else falls back to `human`
   so we never target a dialplan slot that does not exist.
2. The handler returns to the LLM immediately ("Connecting the caller to X now")
   and schedules the real work as a task with **`await asyncio.sleep(3.0)`** —
   the agent's spoken "connecting you now" needs to finish playing, because
   leaving Stasis kills the audio path. The delay stays in the tool handler
   (it is about the announcement), not in the transport (which is about
   mechanism).
3. The handler calls **`call_session.transfer(department)`** — it does not touch
   ARI. `AsteriskCallSession.transfer` then issues
   **`POST /ari/channels/{caller_channel_id}/continue`** with
   `context=transfer` (env `TRANSFER_CONTEXT`), `extension=<department>`,
   `priority=1`.
4. **The department name is the dialplan extension.** Each department needs a
   matching `<name>,1,...` entry in the `[transfer]` context of
   `extensions.conf`, which `Dial()`s the endpoint.
5. Leaving Stasis drops the External Media leg, which ends this call's pipeline
   cleanly; `StasisEnd` destroys the bridge.
6. **`DIALSTATUS` handling lives in the dialplan**, not in Python: an
   unavailable / busy / no-answer human gets a spoken "no one available"
   message instead of a silent drop.

---

## Configuration today (pre-Phase-4)

Everything is either an env var or a hardcoded constant — **there is no config
file yet**, and prompt/voice/model changes require editing `bot.py`.

| Env var | Default | Notes |
|---|---|---|
| `DEEPGRAM_API_KEY` | — | required; STT **and** TTS |
| `GEMINI_API_KEY` | — | required |
| `AUDIOSOCKET_HOST` | `0.0.0.0` | listening address |
| `AUDIOSOCKET_PORT` | `8090` | also given to ARI as the media port |
| `ARI_BASE_URL` | `http://localhost:8088` | |
| `ARI_APP` | `voiceagent` | Stasis app name |
| `ARI_USER` | `voiceagent` | matches `ari.conf` |
| `ARI_PASSWORD` | — | **ARI is enabled only if this is set** |
| `TRANSFER_CONTEXT` | `transfer` | dialplan context for transfers |

`check_keys()` fails fast at startup on missing Deepgram/Gemini keys. Missing
`ARI_PASSWORD` is not fatal — it silently degrades to Path A (no transfer).

---

## Environment
* Asterisk **20.17.0**, Stasis app `voiceagent`.
* Python **3.12**, asyncio, Pipecat **1.6.0** (pinned in `requirements.txt` as of
  2026-07-30; the VM and laptop were split 1.6.0/1.5.0 before that).
* Pipecat is an **in-process BSD-2 library**, not a service. The only real
  external dependencies are **Deepgram** and **Google Gemini**.
