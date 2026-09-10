# Decisions

**Append-only.** Newest at the bottom. Never edit or delete an old entry — if a
decision is reversed, add a new entry that supersedes it and link back.

Format: `## NNN — Title` / *Date* / **Decision** / **Why** / **Consequences**.

Related: [[architecture]], [[bugs]], [[runbook]], [[changelog]], [[flow]], [[roadmap]].

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

---

## 026 — Capacity is derived from the persona roster, not configured separately
*Date: 2026-07-30*

**Decision.** N — the maximum number of simultaneous calls — is `len(pool.personas)`.
There is no `max_concurrent_calls` setting. A persona is one agent, an agent takes
one call, so the roster *is* the capacity.

**Why.** Two numbers that must agree are two numbers that eventually will not.
A separate cap invites `capacity: 5` over a roster of three, which fails as
"caller five hears silence" — the pool hands out nothing but the app believed it
had room. Deriving it makes the disagreement unrepresentable.

**Consequences.** Adding an agent raises capacity as a side effect; that is
intended, but it means capacity changes must be checked against provider
concurrency limits (one Deepgram key, one Gemini key, N concurrent streams each
— see [[runbook]]). One number still cannot be derived: the Asterisk dialplan
`GROUP` cap, which cannot read `config.yaml`. It is kept equal by hand and that
obligation is written down in [[runbook]] and [[personas]] rather than trusted
to memory. Superseded only if a persona is ever allowed to take more than one
call at a time.

---

## 027 — A persona is an override of engine settings, not a new object
*Date: 2026-07-30*

**Decision.** `PoolPersona` holds only `name`, `voice`, `system_prompt`,
`company`, `greeting`. Building its engine means `config_for_persona()` returning
a copy of the shared `AppConfig` with those applied, then calling the *existing*
`create_engine()`. Nothing inside the engine knows personas exist.

**Why.** The alternative — a persona-aware engine builder — would be a second
path for constructing an engine, and second paths drift. Every provider, model,
timeout and bug fix would have to be applied twice. As an override, a persona
inherits all of that for free, and the engine keeps a single constructor.

**Consequences.** Anything that must differ per persona has to be expressible as
an engine setting; that is currently true and worth preserving. The dataclasses
are frozen and `dataclasses.replace` copies, so two calls preparing two personas
concurrently cannot interfere and the shared config is never mutated.
`PoolPersona` deliberately holds no conversation state — all state lives in the
per-call engine, which is why a persona can be safely reused by the next caller.

---

## 028 — The pool holds frozen descriptions, and `release()` is idempotent
*Date: 2026-07-31*

**Decision.** `AgentPool` keeps a list of free personas and a dict of busy ones
keyed by name, moved between the two under an `asyncio.Lock`. `acquire()` returns
`None` when full rather than blocking or raising. `release()` returns a persona
only if it was actually busy, returns a bool rather than raising, and does
nothing on a second call.

**Why each part.**

*Returning `None`, not blocking.* "Everyone is busy" is a normal outcome, not an
error. A caller parked on a lock waiting for a slot hears silence; a caller told
the pool is full hears a message and knows to ring back. Blocking would also turn
the busy path into an unbounded queue by accident, which is explicitly a later
project.

*Idempotent release.* The obvious implementation appends to the free list every
time. Release the same persona twice — a retry, an overlapping cleanup path, a
`finally` that runs after an inner one already ran — and that persona is in the
list twice. Capacity silently becomes N+1 and **two concurrent callers can be
handed the same agent**, which is the one thing this class exists to prevent.
Ignoring an unknown release fails in the safe direction: capacity can read one
too low, but nobody is ever double-booked.

*Never raising.* `release()` is called from a `finally`, frequently while an
exception is already propagating. An exception raised there would replace the
original one, and the actual cause of the failure would be lost. Test:
`test_release_never_raises_inside_a_finally`.

*The lock.* Honest position: on one event loop it is not needed today. There is
no `await` between reading `_free` and writing `_busy`, so the critical section
cannot be interrupted. A mutation test confirms this — deleting the lock makes no
test fail. It is kept for the failure it prevents *later*: the day someone adds
an `await` inside that section (a metrics call, a database write), atomicity
disappears silently and the symptom is two callers hearing the same agent,
rarely, under load. That bug would be nearly unreproducible. Making the critical
section explicit costs nothing.

**Consequences.** `acquire()`/`release()` are event-loop-only — `asyncio.Lock` is
not thread-safe, so calling them from the AudioSocket I/O threads
([[decisions]] 001) would corrupt the pool. `stats()` is deliberately lock-free:
it only reads, and no writer can be caught half-done on the same loop.

The pool guarantees *mutual exclusion*, not *isolation*. Isolation comes from
each call building its own engine and discarding it. `PoolPersona` is frozen and
carries no conversation state, which is what makes handing the same persona to a
later caller safe — put something stateful in it and the privacy guarantee is
gone without a single test going red.

---

## 029 — A freed persona is handed out first (last-freed-first, not round-robin)
*Date: 2026-07-31*

**Decision.** `acquire()` pops the end of the free list, so the most recently
released persona is the next one out.

**Why.** The property that actually matters here is that a reused persona starts
a *clean* conversation. Last-freed-first makes that observable in two sequential
calls: hang up on Alex, ring back, get Alex, confirm he remembers nothing.
Round-robin over three personas would need four calls to see the same agent
twice, and a check that tedious is a check that stops being run.

**Consequences.** Under light traffic one persona takes most calls. That costs
nothing — personas are stateless descriptions, not resources that wear — but it
does mean a caller who rings straight back usually gets the same agent, who has
no memory of them. If that reads as uncanny in practice, changing `pop()` to
`pop(0)` makes it round-robin; nothing else depends on the order.

---

## 030 — Tests use stdlib `unittest`, not pytest
*Date: 2026-07-31*

**Decision.** `tests/test_pool.py` uses `unittest.IsolatedAsyncioTestCase`.
Run with `python -m unittest discover -s tests -t . -v`.

**Why.** pytest plus pytest-asyncio would be two dependencies to pin and install
on both the VM and the laptop — and a version split between those two machines
has already caused silent drift once ([[bugs]], the 1.5.0/1.6.0 API divergence).
`IsolatedAsyncioTestCase` gives each test a fresh event loop, which is precisely
what async pool tests need, for nothing.

**Consequences.** No fixtures or parametrisation; a `roster(n)` helper covers it
at this size. Revisit if the suite grows enough that the boilerplate hurts.

---

## 031 — Two capacity gates, and the app-side one is primary
*Date: 2026-07-31*

**Decision.** Capacity is enforced twice: a `GROUP_COUNT` cap in the Asterisk
dialplan that plays a spoken busy message, and `pool.acquire() is None` →
`transport.reject()` in `run_call`. The app-side gate is the real one; the
dialplan gate exists for the message.

**Why not just the dialplan.** It is Asterisk-specific. A transport with no
dialplan — Twilio, or anything else added later — would have no capacity control
at all, and the pool would be the only thing standing between a fourth caller and
an unanswered promise. Capacity is a property of the *service*, so it belongs
where the service is.

**Why not just the app.** By the time a call reaches `run_call` the only honest
thing left is to hang up: there is no persona to speak with, and answering to say
"we're busy" would mean building a second, agentless audio path purely to
apologise. Asterisk can already play a file to an unanswered channel for free.

**Consequences.** Two numbers must be kept equal by hand, because the dialplan
cannot read `config.yaml`. The failure modes are asymmetric and only one is
visible: a cap set too *high* shows up as `POOL FULL` lines in the bot log and
costs a caller a civil message; a cap set too *low* is silent — callers are turned
away while agents sit idle, and nothing anywhere reports it. `Pool: capacity N` is
logged at every startup so the comparison takes one glance. Recorded in
[[runbook]] §4 and [[personas]].

---

## 032 — A transferred call frees its capacity slot
*Date: 2026-07-31*

**Decision.** The `[transfer]` context reassigns the channel with
`Set(GROUP()=transferred)` as its first priority, so a call handed to a human no
longer counts against the `agents` cap.

**Why.** ARI `continue` moves the *same channel* into the transfer context, so
without this the channel keeps its `agents` membership for the whole human
conversation — while the bot released the AI persona the instant it transferred.
The two gates would then be counting different things: the pool measuring AI
agents in use, the dialplan measuring AI-agents-plus-anyone-still-on-a-transferred
call. A busy twenty-minute human call would block a caller from an agent that was
free, and nothing would look wrong anywhere.

**Why reassign rather than clear.** A channel holds one group per category, so
naming a different group unambiguously removes it from `agents` — whereas relying
on `Set(GROUP()=)` to mean "no group" is an assumption about how Asterisk treats
an empty value, and this is not a place to be clever. `GROUP_COUNT(transferred)`
also becomes a free measure of how many callers are with a human.

**Consequences.** Four one-line additions, one per department extension, and they
must be added to any department added later — a missed one leaks a slot for the
length of a human call. `group show channels` is the check, and the three exit
paths (normal, transferred, caller-dropped) are each verified in [[runbook]] §4.

---

## 033 — The busy message uses built-in Asterisk sounds
*Date: 2026-07-31*

**Decision.** The gate plays `all-circuits-busy-now` then `pls-try-call-later`,
both shipped with Asterisk, rather than a custom `all-agents-busy` recording.

**Why.** The obvious `Playback(all-agents-busy)` names a file that **does not
exist** in Asterisk's core sounds. A missing file logs `file does not exist` and
falls through to the next priority — `Hangup()` — reproducing precisely the silent
drop this gate was added to remove, while looking correct in the dialplan. Built-in
sounds make the gate testable the moment it is pasted in, with nothing to record,
convert or copy to the VM.

**Consequences.** The wording is generic telco phrasing, not branded. Swapping in
a recording is one line once an 8 kHz mono file is in
`/var/lib/asterisk/sounds/custom/`, and the line is marked as such in [[runbook]].

---

## 034 — Agents rotate round-robin (SUPERSEDES 029)
*Date: 2026-08-24*

**Decision.** `acquire()` takes from the **front** of the free list (`pop(0)`),
release puts back on the end. The free list is a queue, so the agent who has been
free longest answers next. This reverses [[decisions]] 029, which took the
most-recently-freed agent.

**Why 029 was wrong.** It optimised for a test, not for the product. Taking the
last-freed persona made "a reused agent remembers nothing" checkable in two calls
instead of four — genuinely convenient, and I weighed it as if that were the main
cost. It isn't. Under light traffic, which is *most* traffic, one call finishes
before the next begins, so the same agent went back to the head of the queue and
answered again. The user's report was blunt and correct: **"I never hear Alex."**
With three personas and sequential calls, Daniel answered every time and the
other two were unreachable unless three calls overlapped. A three-persona pool
that only ever speaks as one persona is not a pool — the entire feature was
invisible in normal use.

029 did name this consequence ("under light traffic one persona takes most
calls") and dismissed it as costless because personas are stateless. That reasoned
about the *implementation* — nothing wears out — and ignored the *caller*, for
whom variety is the whole point of the feature.

**Consequences.** Consecutive callers now get different agents, and the roster is
used evenly. A caller who rings straight back gets a *different* agent, which also
removes the mildly uncanny "same agent, no memory of me" that 029 produced.

The cost is the one 029 was avoiding: verifying clean-context-on-reuse now needs
N+1 sequential calls to see the same agent twice, or a temporary one-persona
roster. That is a test procedure, and test procedures are allowed to be slightly
tedious — [[runbook]] §5 step 8 says how.

Locked down by `test_sequential_callers_rotate_through_the_whole_roster`, which
asserts the exact rotation order, so this cannot regress into 029 unnoticed.

---

## 035 — Shutdown drains: refuse, wait, cancel, then tear down
*Date: 2026-09-08*

**Decision.** SIGINT/SIGTERM starts a graceful drain in this order: stop
accepting (callers arriving mid-shutdown are *rejected*), wait
`service.drain_timeout_s` for in-flight calls, cancel whatever remains, and only
**then** stop the transport. A second signal skips the wait.

**Why the order is the decision.** Tearing down the transport first is the
obvious implementation and it is wrong: ending a call cleanly *needs* ARI, to
destroy that call's bridge and media channel, and needs the audio path to close.
Stopping the transport first pulls both out from under the calls we are trying to
end politely, and leaves orphaned channels on the Asterisk side — the exact
outcome a graceful shutdown exists to prevent.

**Why cancelling is safe rather than brutal.** Every call's `finally` still runs
under cancellation, so the persona returns to the pool and the audio path closes
([[decisions]] 028). A cancelled caller loses the rest of their sentence, not
their slot. This is already tested — `test_persona_released_when_the_call_task_is_cancelled`
predates the drain and is what makes the timeout safe to use at all.

**Why a caller arriving mid-shutdown is refused, not answered.** A caller told
"no" can ring back. A caller answered and then cut off mid-conversation cannot
tell what happened. Asterisk keeps connecting until the transport is actually
down, so this branch is reachable, not theoretical.

**Why the second signal matters as much as the first.** Without it, one caller
who never hangs up holds a deploy hostage for the whole timeout, and the operator
who has already asked twice is told to wait.

**Consequences.** `add_signal_handler` is not implemented on Windows' proactor
loop — and Windows is the development machine even though Linux is the target —
so there is a `signal.signal` fallback that hops back onto the loop with
`call_soon_threadsafe`. The drain also gathers every task's result at the end:
an exception nobody retrieves is re-raised at garbage-collection time and would
print a stray traceback in the middle of the shutdown log, exactly where it looks
like the shutdown itself broke. 8 tests in `tests/test_drain.py`.

---

## 036 — The layering invariants are a test, not a claim
*Date: 2026-09-08*

**Decision.** `tests/test_layering.py` enforces, on every test run, that nothing
outside `engine/` imports Pipecat, that `core/` imports no adapter/engine/factory
(against an explicitly declared allow-list), and that `bot.py` and `core/pool.py`
never reference `PipecatEngine` in code.

**Why.** [[architecture]] has described these as "machine-checked" since the
modular refactor, but **no checker was ever committed** — the claim rested on
someone having run a grep once, months ago. A documented invariant with no
enforcement is worse than no claim at all: it is trusted without being true. This
is the seam the whole design rests on, and the one a future second engine
depends on.

**Why `ast`, not grep.** Text search cannot tell code from prose. The first run
proved it: a raw search for "PipecatEngine" failed on `bot.py`, which mentions it
in a *comment* explaining why the startup warm-up is safe. Parsing imports and
`Name`/`Attribute` nodes gets that right, and also catches
`import pipecat.services.x as y`, which a naive `from pipecat` grep misses.

**Consequences.** `core/`'s third-party dependencies are now a declared list in
the test rather than whatever happens to be imported, so adding one is a
deliberate act visible in review. The reader must use `utf-8-sig`:
`transports/audiosocket.py` carries a UTF-8 BOM (a PowerShell redirect somewhere
in its history) which Python's own tokenizer strips but `ast.parse` on a plain
utf-8 read will not. Development-only scratch files are excluded by name.

---

## 037 — A call must be reconstructable from what it leaves behind
*Date: 2026-09-08*

**Decision.** Every artifact a call produces carries the identifiers needed to
join it to every other artifact: `call_id` bound onto every log line via
`logger.contextualize()`, recording filenames keyed by `call_id`, a `call` header
inside both recording files, Asterisk's own `uniqueid`/`linkedid` captured at
StasisStart and exposed as `CallSession.vendor_ids`, and `tenant_id` stamped on
all of it.

**Why this came before the database.** The obvious next step after "we want call
records" is to add a database. It would have been the wrong order: what the
system produced was **not joinable**, so the rows would have been unjoinable too,
just more expensive to fix. Concretely, before this change:

* recording filenames were `<timestamp>-<fresh uuid4[:6]>` — a value that
  appeared nowhere else, in no log line and in neither file's contents;
* neither recording file contained the call id, the caller, the persona, the
  Asterisk channel, the end reason or the duration;
* Asterisk's `uniqueid`/`linkedid` were never read at all, so nothing could ever
  be joined to a CDR, a CEL row, or a carrier's records;
* the VAD lines, the `CALLER:` transcripts, the DTMF events and the audio
  heartbeats carried nothing identifying the call, so with three calls up they
  interleaved into something unattributable.

You could read a transcript and not know whose it was.

**Why the vendor ids specifically.** Every identifier this process mints is
meaningless outside it. `uniqueid`/`linkedid` are Asterisk's, and they are the
only bridge to anything downstream. They also **cannot be backfilled**: once the
channel is gone nothing here can work out which CDR row was this call. That makes
capturing them a now-or-never decision, which is why it landed in the stage
before the one that needs them. `linkedid` is the one that survives a transfer,
so it is what stitches a transferred call back together.

**Why `contextualize` rather than passing a logger around.** It binds into a
contextvar, and each call is already its own task, so *everything* inside that
task inherits the call id — including the engine and the transcript recorder,
neither of which has to know logging setup exists. The exception is the two
AudioSocket I/O threads: `threading.Thread` does not copy the caller's context,
so they bind explicitly through `AudioSocketConnection.log`. That exception is
the price of [[decisions]] 001 and is cheaper than the alternative.

**Consequences.** `core/` gained a dependency on `loguru`, which the layering
test caught on its first run after this change — working exactly as intended, and
now recorded in that test's declared allow-list rather than assumed. The JSON log
holds caller numbers and transcribed speech, so it is git-ignored and needs the
same retention policy as `recordings/` — currently neither has one, which is
recorded as a gap rather than solved here.

The per-call `[call_id]` prefixes were removed from the log messages themselves,
since the sink now carries the field; the console shows the first 8 characters
for readability while the JSON keeps the full value for joining.

---

## 038 — Overload signals are surfaced, not just counted
*Date: 2026-09-08*

**Decision.** `frames_dropped` (inbound frames discarded when the pipeline falls
behind) and a new `pacer_slips` (the 20 ms write pacer falling >100 ms behind and
resyncing) appear in the per-call closing line and in the 5-second heartbeat —
but only when non-zero.

**Why.** `frames_dropped` was already being incremented and was **read nowhere**:
not logged, not in `stats()`, not exported. The pacer's resync was detected and
silently discarded. Between them they are the two best pieces of evidence this
system has that it is being asked for more than it can deliver — one for each
direction of audio — and both were invisible by accident rather than by design.

This matters now specifically because Stage E has to find the single-node
ceiling. **You cannot find a ceiling you cannot see**: without these, an
overloaded node degrades every call slightly and goes on looking healthy, and the
load test that was meant to find the limit reports a number that is too high.

**Consequences.** Shown only when non-zero, so a healthy call's closing line
stays short and anything appearing there is worth reading. They are per-call
counters, not process-wide — aggregating them is Stage D's job, once there is
somewhere to aggregate into.

---

## 039 — Call records go to SQLite first, not Postgres (SUPERSEDES the plan's choice)
*Date: 2026-09-09*

**Decision.** `CallStore` is an interface; the shipped implementation is stdlib
`sqlite3`. Postgres becomes a second implementation behind the same interface
when there is an instance to verify it against.

**Why this reverses the plan.** The call-centre plan named PostgreSQL, reasoning
that Stage F needs concurrent writers from several nodes and that switching later
costs a migration. That reasoning is still right *for Stage F*. It was written
before checking whether a Postgres instance existed to develop against — and
there is none, on the VM or the laptop.

Writing `asyncpg` code that has never been run, against a database that does not
exist, and landing it in a service that answers real calls, is exactly the
mistake [[decisions]] 025 was written about: unverifiable code proves nothing
while still costing maintenance. That entry deferred a whole phase on this
principle; applying it to a database and not to a transport would be arbitrary.

**What it buys.** Real persistence and real queries now, on the single node this
service actually is, with the *schema*, the *record shapes* and the *writer*
settled and tested before the driver question matters. Those are the parts that
would be expensive to get wrong; the driver is the cheap part.

**What it costs.** SQLite takes one writer at a time. Fine here — writes are a
handful per call and already funnel through a single writer task — but it is not
the answer for Stage F. The migration when it comes is two `CREATE TABLE`s and a
row copy, plus one new file implementing the same four methods. Placeholders and
a couple of DDL keywords differ; nothing above the store changes.

**Consequences.** `service.records.backend` is a config choice with one valid
value today, so adding Postgres is additive rather than a rewrite. WAL mode plus
`synchronous=NORMAL` means a crash can lose the last few records — the right
trade for evidence, and it keeps the writer off the disk's critical path. The
database holds caller numbers and transcribed speech, so it is git-ignored and
inherits the retention gap already recorded for `recordings/` and `logs/`.

---

## 040 — The record path fails soft, always
*Date: 2026-09-09*

**Decision.** `RecordWriter.submit()` is synchronous, non-blocking and never
raises. Records go on a **bounded** queue that a single background task drains.
A full queue drops records with a warning. A store error is logged and the writer
keeps going. Records disabled is a `NullCallStore`, not a `None`.

**Why, in one line.** A record is evidence; the caller is real. Losing a row is
bad, dropping a call because a database was slow is worse.

Each part earns its place against a specific failure:

* **Non-blocking submit.** Awaiting the store from `run_call` would make a
  database problem into a telephony problem — a slow disk would show up as dead
  air. It also puts I/O on the event loop, which is what dropped calls in
  [[bugs]] B-001 and B-011.
* **Bounded queue.** If records are produced faster than the store accepts them,
  the options are: block (slow down calls), grow without limit (kill the process
  eventually), or drop and say so. Only the third fails in a direction the caller
  never notices. The drop is logged at WARNING because it means the analytics
  built on these rows are now quietly incomplete.
* **Errors swallowed in the drain loop.** If the writer task died on the first
  bad row, recording would stop silently for the life of the process — much
  worse than losing one row, and undetectable until someone went looking.
* **A null store rather than a null check.** Otherwise every call site has to ask
  whether recording is enabled, and the fifth one added will forget.

**Consequences.** Dropped and failed counts are tracked in `WriterStats` so the
failure is visible rather than inferred; surfacing them on `/metrics` is Stage
D's job. `close()` drains rather than cancels, because at shutdown the queue
holds the calls that just ended. `core/records.py` takes its logger by injection
rather than importing one, so the contract stays free of the logging setup.

---

## 041 — Our call records do not replace Asterisk's CDR, and cannot
*Date: 2026-09-09*

**Decision.** Keep both. `calls` records what happened *inside* a call; CDR
records every call *attempt*. `uniqueid` is the bridge. For telephony truth —
did it connect, how long was it billable, who dialled — **CDR is authoritative
and our row is a worse copy.**

**Why the question came up.** Asterisk already writes CDR, so a second per-call
table looks like duplication. It is, partly — and the overlap is worth being
explicit about rather than discovering later when two numbers disagree.

**What only CDR has.** `answer` time, `billsec`, `disposition`, and — the one
that matters most — **every call we never saw**. A caller rejected by the
dialplan capacity gate hears the busy message and hangs up without ever entering
Stasis, so `run_call` never runs and no row of ours exists. As far as our
database is concerned that caller did not happen.

That is not a small gap. [[roadmap]] §3 says the trigger for building a queue is
"callers regularly hit the busy message" — and **that is measurable only from
CDR**. On this install (csv backend, `Log unanswered calls: Yes`):

```bash
grep Playback /var/log/asterisk/cdr-csv/Master.csv | grep -c busy
```

**What only we have.** Which persona answered, their voice and model, why the
call ended in our terms (`cause`), whether the LLM chose to transfer and to
which department, `frames_dropped` / `pacer_slips`, `tenant_id`, `node_id`, and
the transcripts. None of it exists anywhere in Asterisk, because all of it is
above the telephony layer. CDR shows a Dial to `PJSIP/102`; it cannot tell you
the LLM decided "billing" — and three departments dial that same endpoint here,
so the destination is genuinely ambiguous from CDR alone.

**Why we still duplicate `duration_s` and `caller_id`.** Two reasons that
outweigh the redundancy:

1. **`CallRecord` is written by `run_call`, which is transport-neutral.** A
   record that leaned on CDR would leave a Twilio call with no record at all,
   and the `BaseTransport` seam would have leaked into the data layer.
2. **CDR here is a CSV file.** `Master.csv` has no indexes and is eventually
   rotated away; it is a fine archive and a poor lookup table. Routine questions
   should not require parsing it.

**Consequences.** The two can disagree; when they do, CDR wins for billing and
connection facts. `Adaptive ODBC` is already a registered backend on this
install, so the better long-term arrangement is to point Asterisk's CDR at the
same database once Stage F introduces Postgres — then `JOIN calls USING
(uniqueid)` is native and the duplicated columns can be reconsidered on evidence
rather than argument. Not worth doing while CDR is CSV and our store is SQLite.

---

## 042 — `Engine.run()` returns a result instead of writing the record itself
*Date: 2026-09-09*

**Decision.** `Engine.run(session)` now returns an `EngineResult` (or None):
`cause`, `transferred_to`, the two transcript paths, and a turn count.
`run_call` builds and submits the `CallRecord`.

**Why the row is assembled outside the engine.** Three parties know different
parts of a call, and only one of them owns its lifecycle:

* the **transport** knows the identifiers and the frame counters,
* the **pool** knows which agent took it,
* the **engine** knows why it ended in conversational terms, whether the model
  chose to hand the caller over, and where the files went.

`run_call` already owns the `finally` where the call is definitively over, so it
is the only place that can write a row carrying *final* counters and an end
reason rather than a snapshot from halfway through. Letting the engine write the
record would also mean every future engine needs to know about stores, writers
and record shapes — precisely the coupling the `Engine` seam exists to prevent.

**Why a return value rather than a mutable out-parameter.** Passing an object
for the engine to fill would be invisible coupling, and would leave no way to
tell "the engine did not set this" from "the engine set it to nothing". A return
value makes the contract explicit and keeps `EngineResult` frozen. `None` is
allowed so an engine with nothing to report is still a valid engine.

**Why `transferred_to` is captured in the tool and not inferred.** The tool
handler is the only place that knows the model *chose* a department. From
outside, a transfer to billing and one to support are indistinguishable here —
three departments dial the same endpoint. It is set when the handover is
initiated, before the three-second announcement sleep, so a process torn down
during those seconds still records what the caller was told was happening.

**Consequences.** `EngineResult` uses no Pipecat vocabulary, so the contract
stays engine-neutral; `tests/test_layering.py` still passes. A crashed engine
returns None and the row is written anyway with
`cause = "engine failed before it could report"` — a call that failed deserves a
record more than one that went fine, not less. `CallSession` gained
`io_counters()` alongside `vendor_ids`, both defaulting to empty so an adapter
that tracks nothing still works.

---

## 043 — The control plane is read-only, in-process, and bound to loopback
*Date: 2026-09-10*

**Decision.** A small aiohttp server in the same process, on the same event
loop, serving `/health`, `/pool`, `/calls` and `/metrics`. No endpoint changes
anything. It binds `127.0.0.1` by default.

**Why in-process.** The state worth exposing — who is free, who is on a call
right now — lives in memory here and nowhere else. A separate service would need
to be told about it, which means either shared storage (a new dependency, and a
new thing that can be stale) or the call path doing extra work to publish it. A
handler that reads a dictionary costs nothing. `aiohttp` is already a dependency,
used as a client by ARI, so this adds nothing to install.

**What sharing the loop costs, and the rule it forces.** A slow handler is a
dropped call. This codebase has lost calls to event-loop stalls twice ([[bugs]]
B-001, B-011), and a Prometheus scrape every fifteen seconds is a dependable way
to find a third. So **every handler reads memory and returns**: no database
queries, no file I/O, nothing awaited that can be slow. Historical questions are
a query against `records/calls.db` run by whoever is asking. If a handler ever
needs real work it moves off the loop or out of the process — it does not get
"just this one await". aiohttp's access log is disabled for the same reason.

**Why read-only.** There is no endpoint to hang up a call, reload config or take
an agent out of the pool. A control plane that can *act* needs authentication,
and this has none. Read-only keeps the blast radius of that decision to
disclosure rather than disruption.

**Why loopback, specifically.** `/calls` returns **caller phone numbers**.
Binding `0.0.0.0` publishes personal data to anyone who can reach the port, with
no credential required. Loopback plus an SSH tunnel or an authenticating proxy is
the intended remote-access story. Binding wider is not refused — there are
legitimate reasons — but it logs a warning, because the one thing that must not
happen is it changing by accident and nobody noticing.

**Consequences.** `/metrics` deliberately carries **numbers only**, no call ids,
personas or caller numbers: a metrics endpoint is the one most likely to be
scraped into a system with looser access rules than this one. `/health` returns
**200 with `status: at_capacity`** when the pool is full — a busy node is doing
its job, and a load balancer must not pull it out for that; it goes unhealthy
only when something is actually wrong. The API starts *after* the transport and
stops *before* the drain, so it can never report ready while nothing can answer,
nor healthy while the service is on its way out.

---

## 044 — Live call state is separate from the pool
*Date: 2026-09-10*

**Decision.** `core/live.py` holds `LiveCalls` (who is on a call, with caller,
agent and duration) and `Counters` (totals since boot). The pool is untouched.

**Why not extend the pool.** `PoolStats` knows Sarah is busy; it does not know
who she is talking to or for how long, and adding that would put display
concerns inside the one class where a mistake double-books a caller.
`core/pool.py` is small on purpose.

**Why not read the database.** A call's row is written in the `finally`, so a
call *in progress* does not appear in it at all. "Who is on a call right now" is
a question only in-memory state can answer — and answering it from the database
would mean a query on the event loop, which the previous decision forbids.

**Consequences.** `LiveCalls` uses a plain `threading.Lock`, not an
`asyncio.Lock`: the critical sections are three dictionary operations, so a
blocking lock held for nanoseconds is safe on the loop, whereas an asyncio lock
would make every read a coroutine and make it unusable from the I/O threads that
already exist. `live.ended()` is called **first** in the call's `finally`, before
anything that can fail — a call still showing as in progress after it ended is
the one way this display can actively mislead, and removing it must not depend on
the rest of the teardown succeeding. Counters are monotonic only; anything
current (free agents, calls in flight) is read live at scrape time, because a
counter can be scraped at any interval and still give a correct rate while a
sampled gauge can miss a spike entirely.

---

## 045 — The dashboard is pushed only when something changes
*Date: 2026-09-10*

**Decision.** `WS /live` sends the full state on connect, then again only when a
**change signature** differs — and that signature deliberately excludes every
value that ticks on its own: uptime, and each call's duration. The browser
derives durations locally from `started_at`.

**Why.** The obvious implementation pushes a snapshot every second. On a service
with no calls that is a message a second, forever, per watcher — pure event-loop
work in the same process that answers phone calls, to tell a dashboard that
nothing happened. Excluding the ticking values means **an idle service sends
nothing at all**, and a dashboard left open overnight costs one string comparison
a second.

It also renders better: a duration counted by the browser advances smoothly,
where one refreshed by a server push jumps whenever the network hiccups.

**Consequences.** The change signature is now something that has to be kept
honest — add a field that ticks and the socket goes back to chattering. Two tests
pin it: one asserting a changed duration and uptime do *not* count as a change,
one asserting an idle service pushes nothing for three seconds.

One shared broadcast task serves every connected socket, so the snapshot is
computed once per tick regardless of how many people are watching. The signature
is shared rather than per-socket, which is why a new connection sets it after its
initial send — without that, the first tick after connecting resent the identical
state, which is exactly what the idle test caught.

The socket is **output only**: incoming frames are drained so aiohttp can process
pings and closes, and discarded. Accepting commands would make it a control
channel, and there is no authentication ([[decisions]] 043).

**The page itself** is one self-contained HTML file with no build step, no
framework and no CDN. It is served from memory by the API, on a VM that may have
no outbound internet — and a dashboard that needs `npm` to change is a dashboard
nobody changes. A test asserts it contains no external `<script src=>`.
