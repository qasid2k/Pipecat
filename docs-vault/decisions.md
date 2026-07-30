# Decisions

**Append-only.** Newest at the bottom. Never edit or delete an old entry — if a
decision is reversed, add a new entry that supersedes it and link back.

Format: `## NNN — Title` / *Date* / **Decision** / **Why** / **Consequences**.

Related: [[architecture]], [[bugs]], [[runbook]], [[changelog]].

---

## 001 — Socket I/O runs on dedicated OS threads, not the event loop
*Date: pre-2026-07-30 (recovered from code comments)*

**Decision.** Each call's AudioSocket reads and writes happen on two blocking
OS threads. They bridge to asyncio via `loop.call_soon_threadsafe` and queues.

**Why.** Asterisk's AudioSocket writer is non-blocking with zero tolerance: if
it cannot hand a frame over the instant it is ready, it abandons the call. On the
event loop, any stall — ONNX model loading, GC, LLM/TTS inference — was long
enough to fill Asterisk's TCP window and drop the call. Blocking `recv()`
releases the GIL, so the socket is drained continuously no matter what the
pipeline is doing.

**Consequences.** N calls = N pipelines + 2N threads. Any refactor must preserve
the threads *and* the lockstep timing; this is the most dangerous code to touch.
Carried forward deliberately into Phase 2 (`AsteriskCallSession` keeps the
threads internally and only *exposes* async `read_audio`/`write_audio`).

---

## 002 — Send audio continuously, emitting silence when idle
*Date: pre-2026-07-30 (recovered from code comments)*

**Decision.** The write thread sends a frame every 20 ms forever — real agent
audio when there is some, otherwise 320 bytes of silence.

**Why.** AudioSocket is lockstep. If we stop sending, Asterisk stops forwarding
the caller's audio and the call goes deaf in both directions.

**Consequences.** "Idle" is not free; there is always traffic on the socket. The
outgoing queue is bounded at 3 frames so the blocking `put()` back-pressure is
what paces the agent's speech to real time. Do not "optimise" the silence away.

---

## 003 — Run the entire pipeline at 8 kHz, with no resampling
*Date: pre-2026-07-30*

**Decision.** 8 kHz / 16-bit / mono slin is the canonical format everywhere:
socket, VAD, STT, LLM, TTS.

**Why.** Telephony is 8 kHz. Deepgram STT, Deepgram Aura-2 TTS and Silero VAD all
accept 8 kHz natively, so resampling would add CPU and latency to buy nothing.

**Consequences.** The Asterisk adapter needs **zero** audio conversion. Future
cloud transports (e.g. Twilio's 8 kHz mu-law over WebSocket) must convert to and
from this canonical format *inside the adapter*; the core never sees a vendor
format.

---

## 004 — LLM is `gemini-flash-lite-latest`, not a pinned version, not `flash`
*Date: pre-2026-07-30 (recovered from code comments)*

**Decision.** `gemini-flash-lite-latest`.

**Why.** Two separate choices. **`flash-lite` over `flash`**: measured ~700 ms vs
~1700 ms on this account, and 1.7 s of dead air destroys the feel of a phone
call. **`-latest` over a pinned version**: `gemini-2.5-flash` was retired by
Google and started returning 404; the alias survives that.

**Consequences.** Google can change the model under the alias, so behaviour may
drift without a code change. Accepted: an unannounced quality shift is cheaper
than an outage. If more reasoning power is ever needed, the latency cost must be
measured on a real call, not assumed.

---

## 005 — Turn-taking is Silero VAD + a 0.6 s silence timeout; Smart Turn v3 off
*Date: pre-2026-07-30 (recovered from code comments)*

**Decision.** `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)`,
replacing Pipecat's default Smart Turn v3.

**Why.** Smart Turn v3 loads a second ONNX model and runs inference on every
utterance — CPU and latency we do not need, on a box that also drops calls when
it stalls (see 001). Silero VAD already reports speech start/stop, so "0.6 s of
quiet after speech" needs no model at all.

**Consequences.** Slightly blunter end-of-turn detection: a caller who pauses
mid-thought for >0.6 s gets interrupted. 0.6 s is a tuned trade-off — it becomes
a config knob in Phase 4, which is the right place to experiment with it.

---

## 006 — Transfer is `channels/{id}/continue` into a dialplan context
*Date: pre-2026-07-30*

**Decision.** The LLM tool picks a department; the department name is used as the
dialplan **extension** in `POST /ari/channels/{id}/continue` with context
`transfer`. `extensions.conf` decides who each department actually dials.

**Why.** It puts routing where a telephony admin expects it and can change it
without touching Python. It also exits Stasis cleanly, which tears the AI audio
path down on its own instead of us having to unpick the bridge mid-call.

**Consequences.** Every department in the tool's enum **must** have a matching
`<name>,1,...` entry in `[transfer]` or the transfer fails silently from the
LLM's point of view. Hence the hard `human` fallback for unknown values.

---

## 007 — `DIALSTATUS` handling stays in the dialplan
*Date: pre-2026-07-30*

**Decision.** "No one available" (busy / no answer / unavailable) is handled by
dialplan `DIALSTATUS` branching, not by Python.

**Why.** Once the channel leaves Stasis we no longer own it; the dialplan does.
Trying to observe and recover the outcome from Python would mean re-entering
Stasis and rebuilding the audio path for a message we can simply `Playback()`.

**Consequences.** Part of the product's behaviour lives in Asterisk config,
outside this repo. It must be documented in [[runbook]] and must survive every
refactor — it is one of the checkpoint tests.

---

## 008 — Transfer waits 3 s before leaving Stasis
*Date: pre-2026-07-30*

**Decision.** The tool handler returns to the LLM immediately and does the real
ARI call in a background task after `asyncio.sleep(3.0)`.

**Why.** Leaving Stasis kills the audio path instantly. Without the delay the
caller is cut off in the middle of "connecting you now" and the transfer feels
broken even though it worked.

**Consequences.** A magic number tied to the length of one spoken sentence. If
the greeting/handoff wording or the TTS voice changes materially, re-check it.
Ideally this would wait on a "TTS finished" signal rather than a fixed sleep —
noted as a future improvement, not being changed during the refactor.

---

## 009 — Pipecat stays; we wrap it rather than abstract each provider
*Date: 2026-07-30*

**Decision.** Pipecat remains the conversation engine. We isolate it behind
**one** interface of our own (`Engine`, Phase 3) and build no per-provider
wrapper classes of our own.

**Why.** Pipecat is an in-process BSD-2 library, not an external service, and it
*already* abstracts STT/LLM/TTS providers. A second abstraction layer over
`DeepgramSTTService` etc. would be pure duplication. The real risk we are
insuring against is "Pipecat itself becomes the wrong choice" — one seam covers
that. "Configurable services" therefore means the engine **constructs** Pipecat
services from config values (provider, model, voice), not that we re-wrap them.

**Consequences.** Swapping STT/LLM/TTS is a config change limited to Pipecat's
supported providers. Leaving Pipecat entirely means one new `Engine`
implementation, and nothing outside it changes.

---

## 010 — FreePBX needs no separate adapter
*Date: 2026-07-30*

**Decision.** The Asterisk adapter covers FreePBX.

**Why.** FreePBX is a management UI on top of Asterisk; ARI, Stasis and
AudioSocket are identical underneath.

**Consequences.** Only the dialplan/config *authoring* differs (via the FreePBX
UI and its custom-context conventions). That is a runbook concern, not a code
concern.

---

## 011 — Secrets in the environment, never in `config.yaml`
*Date: 2026-07-30*

**Decision.** `config.yaml` holds the **names** of env vars (`api_key_env:
DEEPGRAM_API_KEY`), never values. Secrets live in `.env` / the environment.
`config.yaml` is safe to commit; `.env` is git-ignored.

**Why.** The config file becomes the thing everyone edits, diffs, pastes into
chat and commits. Keys must not be in it, by construction rather than by
discipline.

**Consequences.** The loader resolves `*_env` names at startup and **fails fast
with a clear message** naming the missing variable. Slightly more indirection
when reading the config, in exchange for a repo that cannot leak a key.

---

## 012 — Pin Pipecat in `requirements.txt`
*Date: 2026-07-30*

**Decision.** `pipecat-ai[deepgram,google,silero]==1.6.0`, pinned, with an
instruction to reinstall on both machines when it is bumped.

**Why.** The VM ran 1.6.0 and the dev laptop 1.5.0 against an unpinned
requirement. Pipecat's API moves between minor versions, so "works on my
machine" was a coin flip. The whole point of the refactor is traceability;
version drift makes a regression untraceable.

**Consequences.** Upgrades are now deliberate and land in [[changelog]].
Verified on 1.6.0: `GoogleLLMService.Settings(model=…)`,
`DeepgramTTSService.Settings(voice=…)`,
`SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=…)`,
`PipelineWorker(idle_timeout_secs=…, app_resources=…)` and
`FunctionSchema(handler=…)` all still exist with the same shapes.
