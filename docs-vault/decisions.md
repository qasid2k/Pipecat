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

**Consequences (012).** Upgrades are now deliberate and land in [[changelog]].
Verified on 1.6.0: `GoogleLLMService.Settings(model=…)`,
`DeepgramTTSService.Settings(voice=…)`,
`SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=…)`,
`PipelineWorker(idle_timeout_secs=…, app_resources=…)` and
`FunctionSchema(handler=…)` all still exist with the same shapes.

---

## 013 — The transport contract is `CallSession` + `BaseTransport`, and it is small
*Date: 2026-07-30 (Phase 2)*

**Decision.** Two abstract base classes in `core/transport.py`. `CallSession` is
one live call: `read_audio`, `write_audio`, `transfer`, `hangup`, plus `call_id`
and `caller_id`. `BaseTransport` is one vendor connection: `start`, `stop`,
`listen`, `reject`.

**Why.** Small enough that a new vendor is obviously implementable, and shaped
around what a *call* is rather than what Asterisk happens to do. Frames in,
frames out, plus the two control verbs that actually matter on a phone call.

**Three additions beyond the original sketch, each for a concrete reason:**
* `ended` (an `asyncio.Event`) and `end_reason` — the conversation must know
  when the call is over and log why. Every vendor has this; without it the
  engine would have to poll or reach into vendor internals.
* `can_transfer` — the agent has to tell the caller the truth *before*
  promising a transfer, so this must be answerable synchronously, up front.
  Asterisk returns False for a direct AudioSocket call, which has no ARI
  channel to act on.
* `read_audio()` returns `None` exactly once at end of call. A sentinel rather
  than an exception, because the read loop's normal exit is not an error.

**Consequences.** The pipeline no longer imports anything vendor-specific.
`bot.py` lost the listening socket, the accept thread, the ARI controller and
the UUID correlation — all of it moved into the adapter. Verified in a
socket-level harness: 26/26 checks including 20.0 ms pacing and 50 silence
frames/s.

---

## 014 — `hangup()` releases the audio path only; it never hangs up the channel
*Date: 2026-07-30 (Phase 2)*

**Decision.** `AsteriskCallSession.hangup()` calls `io.stop()` and nothing else.
It does **not** issue ARI `DELETE /channels/{id}`. It is idempotent.

**Why.** `hangup()` is called from the engine's `finally`, so it runs on *every*
exit path — including after a successful transfer. At that moment the channel has
left Stasis, belongs to the dialplan, and may already be connected to a human.
An ARI hangup there would cut off the very call we just transferred. Bridge and
External Media cleanup is already handled by the `StasisEnd` handler, which fires
however the call ends, so nothing is leaked by staying out of it.

**Consequences.** This is a *load-bearing omission* — the kind a future
"cleanup" refactor would happily add back and break transfer with. Hence the
comment in the code, the warning in [[architecture]], and a regression check
asserting that no ARI hangup follows a transfer.

---

## 015 — Per-call intake tasks, so correlation never serialises calls
*Date: 2026-07-30 (Phase 2)*

**Decision.** `AsteriskTransport` does not do per-call setup inside `listen()`.
The accept thread dispatches an `_intake()` task per connection, which starts the
I/O threads, waits up to 2 s for the UUID correlation, and then puts a finished
session on a queue that `listen()` drains.

**Why.** The UUID correlation involves a wait of up to 2 s. Done inline in the
`listen()` loop, call N+1 would sit behind call N — and audio is already flowing
from the instant Asterisk connects, so a delay is not harmless. Before the
refactor this parallelism came for free, because each call was its own coroutine;
the refactor had to reproduce it deliberately.

**Consequences.** The same shape is now required of every future transport, so it
is written into `BaseTransport.listen()`'s docstring as a rule, not a suggestion.
Also note the main loop must use `asyncio.create_task(handle_call(...))`, not
`await` — the Phase 4 sketch in the project brief shows a plain `await`, which
would serialise calls; it needs a task there too.

---

## 016 — The Pipecat glue sits on a `CallSession`, not on the raw connection
*Date: 2026-07-30 (Phase 2)*

**Decision.** `AudioSocketInputTransport` / `AudioSocketOutputTransport` take a
`CallSession` and call `read_audio()` / `write_audio()` instead of touching
`AudioSocketConnection` directly.

**Why.** The alternative — leaving the glue on the raw connection and adding the
interface alongside — would have made `read_audio`/`write_audio` dead code that
nothing exercises, so a bug in them would only surface in Phase 5 with Twilio.
Routing the real audio through the interface proves it works now, and it means
the Pipecat glue is already vendor-neutral: any `CallSession` can drive a
pipeline through it.

**Consequences.** One extra method call per 20 ms frame — immeasurable next to
STT/LLM/TTS. The mechanics underneath are unchanged: same queues, same threads,
same bounded-put back-pressure. These classes are no longer Asterisk-specific and
move next to the engine in Phase 3.

---

## 017 — ARI event handlers run as tasks, not inline on the WebSocket loop
*Date: 2026-07-30 (fixing [[bugs]] B-010)*

**Decision.** `_dispatch` spawns `_on_stasis_start` / `_on_stasis_end` as tasks.
The one exception is our own media channel's `StasisStart`, handled inline
because it only opens a gate and has nothing to await.

**Why.** Call setup has to *wait for another ARI event* — the media channel's
`StasisStart` — before it can bridge that channel. Handled inline, the loop would
be blocked inside the handler waiting for an event only that same loop could
read: a guaranteed deadlock on every call. Concurrency here is a requirement, not
an optimisation.

**Consequences, and they are not free.** Handlers can now interleave, which the
old serialized code never allowed. That immediately produced a new failure mode
(the phantom-call bug, B-010(b)): teardown during setup discarded the media
channel id, and its in-flight `StasisStart` was then treated as a new incoming
call. Two rules now hold and must not be quietly undone:
* An id stays in `_em_ids` until the channel's **final** event (`StasisEnd`).
  Never retire it while an event could still be in flight.
* Setup re-checks that its registry entry still exists after any await, because
  the caller may have hung up meanwhile.

A per-call lock would be the heavier alternative; it is not needed as long as
those two rules hold, and both are covered by regression checks. Setup for
simultaneous calls is now genuinely parallel rather than serialized, which is a
small side benefit.

---

## 018 — The Engine contract is one method: `run(session)`
*Date: 2026-07-30 (Phase 3)*

**Decision.** `core/engine.py` defines `Engine` with a single abstract method,
`async run(session) -> None`, which talks to the caller until the call ends.
`PipecatEngine` implements it. See 009 for *why* Pipecat is wrapped at all.

**Why so small.** The interface exists to make one specific swap possible —
replacing Pipecat — and nothing else. Anything richer (turn events, hooks,
barge-in callbacks) would be inventing requirements we do not have, and every
one of them would leak Pipecat's model of a conversation into the contract,
defeating the purpose. Note what the file does not mention: pipelines, frames,
processors, aggregators, VAD.

**Consequences.** Enforced by two machine checks: nothing outside `engine/`
imports Pipecat, and `core/` imports only `abc`, `typing` and `asyncio`.
`bot.py` fell to ~90 lines of wiring. Swapping Pipecat = one new module in
`engine/` and a changed line in `create_engine()`.

---

## 019 — The caller of `run()` owns the session, not the engine
*Date: 2026-07-30 (Phase 3)*

**Decision.** `Engine.run()` must never call `session.hangup()`. `bot.run_call`
does it in a `finally`.

**Why.** Cleanup-on-every-path is easy to get wrong once per implementation, and
every future engine would have to remember it — including on the exception path.
Putting it in the caller means it is written once and cannot be forgotten. It
also keeps the ownership rule honest: whoever *acquired* the resource releases
it, and the engine never acquired it.

**Consequences.** `run()` must return rather than raise for an ordinary call
ending, which is now stated in its docstring. `run_call` also catches engine
exceptions so one failed call cannot kill the accept loop. Covered by checks that
`hangup` is called exactly once on both the normal and the crash path, and that
`pipecat_engine.py` contains no `hangup` call at all.

---

## 020 — Files moved to match the layering
*Date: 2026-07-30 (Phase 3)*

**Decision.** `audiosocket_transport.py` was split and moved:
its `AudioSocketConnection` half → `transports/audiosocket.py`, its Pipecat glue
→ `engine/session_transport.py` (renamed `CallSession*Transport`). The persona,
pipeline, tool and transcript code moved from `bot.py` into `engine/`.

**Why.** The old name had become a lie — after Phase 2 the file was neither
purely AudioSocket nor purely a transport, and it was the only place where
vendor code and Pipecat code still sat side by side. The Pipecat glue in
particular was never really Asterisk-specific: it will drive a pipeline from any
`CallSession`, which is exactly what Phase 5 needs for Twilio.

**Consequences.** Moved with `git mv`, so history follows. One import line
changed in the Asterisk adapter. The layout now states the architecture:
`core/` contracts, `transports/` vendors, `engine/` conversation.

---

## 021 — Unknown config keys are an ERROR, not a warning
*Date: 2026-07-30 (Phase 4)*

**Decision.** The loader rejects any key it does not recognise, naming the
dotted path and listing the valid keys for that section.

**Why.** The alternative — ignore what you don't understand — turns a typo into
a silent no-op. `voicce: aura-2-thalia-en` would leave the old voice in place,
and the person who edited it would be left with "I changed the config and
nothing happened", which is a genuinely horrible thing to debug because there is
no evidence anywhere. Strictness converts that into a startup error that points
at the line.

**Consequences.** Adding a new setting means adding it to the `allowed` set as
well as reading it — deliberate friction, in the right direction. Every setting
is validated before anything starts listening, so a bad config can never surface
mid-call.

---

## 022 — Missing ARI credentials now fail fast instead of degrading silently
*Date: 2026-07-30 (Phase 4)*

**Decision.** If `config.yaml` names `ari_pass_env`, that variable must exist or
the bot refuses to start. To run without call control you comment the key out —
an explicit choice rather than an accident.

**Why.** Previously an unset `ARI_PASSWORD` started the bot happily with
transfer quietly broken. That was already documented in [[runbook]] as the first
thing to check when "transfer stopped working" — which is exactly the shape of a
bug that should be impossible rather than documented.

**Consequences.** A behaviour change, and the one most likely to surprise: a dev
laptop with no ARI credentials will now refuse to start. The escape hatch is
`config.local.yaml` (git-ignored) or `$VOICEAGENT_CONFIG`. Worth it — "fails
loudly at startup" beats "works, but one feature is missing".

---

## 023 — The factories live outside `core/`
*Date: 2026-07-30 (Phase 4)*

**Decision.** `create_transport` / `create_engine` are in a top-level
`factories.py`, not in `core/`. Implementations are imported lazily inside the
functions.

**Why.** A factory must import every implementation it can build. In `core/`
that would mean core imports Asterisk and Pipecat, and the layering that
Phases 2–3 established would be gone — the check that `core/` imports only
`abc`, `typing` and `asyncio` would fail. The factory belongs *above* the
layers, next to `bot.py`, the one place allowed to know what is actually being
run. Lazy imports mean an unused vendor's dependencies never need installing.

**Consequences.** One module knows every name; everything else knows only the
contracts. Adding a vendor is a new module plus one branch here.

---

## 024 — The persona prompt is a file with placeholders
*Date: 2026-07-30 (Phase 4)*

**Decision.** `prompts/alex.txt`, with `{name}` and `{company}` substituted from
`persona.name` / `persona.company`. Substitution is `str.replace`, not
`str.format`.

**Why.** A prompt is prose, and prose belongs in a text file where it can be
edited and diffed without touching Python or worrying about quoting. The
placeholders keep `persona.name` meaningful rather than decorative. `str.replace`
because a stray `{` in prompt text must not raise — prompts get pasted in from
all sorts of places, and a formatting crash at startup over a curly brace would
be an absurd failure mode.

**Consequences.** Verified that the file's text, after substitution, is
byte-identical to the prompt that was hardcoded before, so the agent's behaviour
is genuinely unchanged. An inline `system_prompt` is still allowed for short
experiments; setting both is an error.

---

## 025 — Phase 5 (Twilio) deferred rather than built unverified
*Date: 2026-07-30*

**Decision.** Stop the modular/configurable project after Phase 4. The Twilio
transport is designed and documented in [[runbook]] but not implemented.

**Why.** Phase 5's purpose was never "support Twilio" — it was to *prove* the
transport abstraction by exercising it with a genuinely different vendor. With
no Twilio account available to test against, the code could not have been
verified against a real carrier, and an unverified adapter proves nothing while
still costing maintenance. Every other phase in this project was checked before
it was committed; adding one unproven part would have been the weakest thing in
the repo and the easiest to trust by mistake.

**What that costs.** The abstraction is untested against a second vendor, so its
shape is an informed guess, not a demonstrated fact. The known-shaky points are
recorded in [[runbook]] so the next person meets them deliberately:
`write_audio()` self-pacing, `reject()` finally having a real job, and the
department→number mapping needing to become config because there is no dialplan.

**One design note worth keeping.** The obvious shortcut — reuse Pipecat's Twilio
serializer for mu-law — must be avoided. It would make `transports/` import
Pipecat, undoing [[decisions]] 018 and leaving a vendor's audio path broken if
the engine were ever swapped. `audioop` is no help either: deprecated and removed
in Python 3.13, so leaning on it would silently cap the project's Python
version. G.711 is about forty lines and two lookup tables, and can be proven
bit-exact against `audioop` as a test oracle while never depending on it at
runtime.
