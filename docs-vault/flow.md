# Flow

**What happens, in order, from `python bot.py` to the caller hanging up.**

The other notes explain *why* things are the way they are. This one explains
*what runs when*, so a new reader can follow a call through the code without
opening ten files at once. Read this first, then [[architecture]] for the
layering and [[decisions]] for the reasoning.

Related: [[architecture]], [[decisions]], [[personas]], [[runbook]], [[bugs]],
[[roadmap]].

---

## 1. The whole thing in one picture

```
   PHONE                ASTERISK                    BOT (one Python process)
     |
     |  dial 6001         |
     |------------------->| [dialplan] capacity gate
     |                    |   3 agents busy? --> play busy message, hang up
     |                    |   otherwise: Set(GROUP()=agents), Stasis(voiceagent)
     |                    |
     |                    |---- ARI event: StasisStart --------->|  answer
     |                    |<--- answer / bridge / externalMedia -|  bridge
     |                    |                                      |
     |                    |---- AudioSocket TCP connect -------->|  accept
     |                    |     (first message = the UUID)       |  correlate
     |                    |                                      |
     |                    |                                      v
     |                    |                          pool.acquire() -> Sarah
     |                    |                                      |
     |                    |                          build Sarah's engine
     |                    |                                      |
     |  <== audio ========|========= 20 ms frames ===============|  conversation
     |  ==> audio ========|=====================================>|
     |                    |                                      |
     |                    |<--- ARI continue -> [transfer] ------|  (if transferred)
     |                    |                                      |
     |  hang up           |                                      |
     |------------------->|---- StasisEnd --------------------->|  pool.release()
```

Two connections to Asterisk, doing different jobs. Keeping them straight is the
key to reading this codebase:

| | Carries | Shape | Code |
|---|---|---|---|
| **ARI** | *control* — answer, bridge, transfer, hangup | HTTP + a WebSocket for events | `ari_controller.py` |
| **AudioSocket** | *audio* — raw PCM frames, nothing else | one TCP connection per call | `transports/audiosocket.py` |

ARI never carries audio. AudioSocket never carries control. They are tied
together by a **UUID** the bot generates — see §3.

---

## 2. Startup: `python bot.py`

```
bot.py __main__
  |
  +- load_config()                    core/config.py
  |    read config.yaml, validate EVERYTHING, resolve *_env from .env
  |    -> a bad setting stops the service HERE, not on a call
  |
  +- log the banner: transport, engine, pool capacity, persona names
  |
  +- asyncio.run(main(cfg))
       |
       +- AgentPool(config.pool.personas)          core/pool.py
       |    the roster IS the capacity; 3 personas = 3 simultaneous calls
       |
       +- create_engine_for_persona(...)   <- thrown away on purpose
       |    forces the heavy Pipecat / onnxruntime / google-genai imports NOW,
       |    with nobody on the line. Logs "Engine ready (Ns warm-up)".
       |    Startup is slow here BY DESIGN -- see [[bugs]] B-011.
       |
       +- create_transport(config)                 factories.py
       |
       +- await transport.start()                  transports/asterisk.py
       |    * open the listening TCP socket (SO_RCVBUF set BEFORE bind)
       |    * start the blocking accept() THREAD
       |    * start the ARI WebSocket task
       |
       +- async for session in transport.listen():
              asyncio.create_task(run_call(...))    <- one task per call
```

After this the process sits in that `async for` doing nothing until a call
arrives. Everything below happens per call, concurrently.

---

## 3. A call arriving — the two halves

One inbound call causes **two independent things** to reach the bot, in an order
that is not guaranteed. Most of the complexity in `transports/asterisk.py` and
`ari_controller.py` exists to join them back together.

### Half A — ARI (control), `ari_controller._on_stasis_start`

```
1. Asterisk sends StasisStart for the caller's channel
2. generate  au    = a fresh UUID       <- the correlation key
   generate  em_id = "em-<hex>"         <- id for our media channel
3. create the em_ready gate BEFORE the channel exists
      (so its own StasisStart can never arrive before we can notice it)
4. answer(caller channel)
5. create a mixing bridge, add the caller to it
6. registry[au] = AriCall(...)          <- written BEFORE the media channel,
                                           so correlation cannot lose a race
7. POST /channels/externalMedia   data=au, encapsulation=audiosocket,
                                  connection_type=client, format=slin
      -> Asterisk will now DIAL OUT to our AudioSocket server
8. wait (max 2 s) for the media channel's own StasisStart   <- [[bugs]] B-010
9. add the media channel to the bridge
```

Now the caller and a media channel share a bridge, so the caller's audio has
somewhere to go.

### Half B — AudioSocket (audio), `AsteriskTransport`

```
1. accept() THREAD wakes: Asterisk has connected
2. dispatch _intake(conn, addr) as a task      <- NOT inline; see below
3. AudioSocketConnection(conn).start()
      spawns TWO OS threads for this call:
        read  thread -- blocking recv(), parses frames, pushes to the loop
        write thread -- sends a frame every 20 ms, forever
4. _correlate(): wait (max 2 s) for the FIRST AudioSocket message,
   which is the UUID Asterisk was handed as `data=au`
5. registry.get(uuid) -> the AriCall from Half A
6. build AsteriskCallSession(io, ari_call, ...)
7. put it on the _ready queue  ->  transport.listen() yields it
```

**Why `_intake` is a task, not inline.** It waits up to 2 s for the UUID. Done
inline in the accept loop, call N+1 would queue behind call N's wait, and two
people dialling at once would be serialised. One task each keeps them parallel.

**A direct call to 6000** skips Half A entirely: no ARI channel, so `_correlate`
times out, `ari_call` is `None`, and `can_transfer` is `False`. Conversation
works; transfer does not.

---

## 4. `run_call` — the only path a call can take

`bot.py`. Everything above was plumbing; this is the actual policy.

```python
persona = await pool.acquire()                            # 1
if persona is None:                                       # 2  everyone busy
    await transport.reject(session)
    return
try:
    engine = create_engine_for_persona(config, persona)   # 3  OWN instance
    await engine.run(session)                             # 4  the conversation
finally:
    await pool.release(persona)                           # 5  ALWAYS
    await session.hangup()                                # 6
```

1. **acquire** takes the agent free longest — round-robin, so consecutive
   callers get different people ([[decisions]] 034).
2. **None means full.** Refuse rather than answer badly. On Asterisk the
   dialplan usually caught this first; reaching here means the dialplan cap and
   the roster have drifted ([[runbook]] §4).
3. **A fresh engine, every call** — its own VAD, provider connections and
   conversation context. Built *inside* the `try`, because it can fail after the
   agent has already left the pool.
4. `engine.run()` blocks for the whole conversation.
5. **Release before hangup.** `hangup()` talks to the vendor and can fail; an
   agent released after it would never be released at all.
6. `hangup()` closes the *audio path only* — it deliberately does not hang up
   the caller's channel, which after a transfer may be talking to a human.

---

## 5. Inside the conversation — `PipecatEngine.run()`

The pipeline built per call (names as they appear in the logs):

```
caller audio (320-byte frames, 8 kHz)
      |
      v
CallSessionInputTransport --> VADProcessor --> DeepgramSTTService
   (our CallSession ->            (Silero:          (speech -> text)
    Pipecat frames)          is anyone speaking?)          |
                                                           v
                                                  TranscriptRecorder
                                                           |
                                                           v
                                                   LLMUserAggregator
                                                  (assemble a full turn)
                                                           |
                                                           v
                                                    GoogleLLMService
                                                   (Gemini + the tool)
                                                           |
                                                           v
                                                  DeepgramTTSService
                                                     (text -> speech)
                                                           |
                                                           v
                                           CallSessionOutputTransport
                                                           |
                                                           v
                                                LLMAssistantAggregator
                                              (remember what we said)
```

The turn cycle:

1. The **write thread** is already sending 20 ms frames — real audio when there
   is some, silence otherwise. It never stops: AudioSocket is lockstep, and if
   we stop sending, Asterisk stops forwarding the caller ([[decisions]] 002).
2. On answer the greeting is spoken immediately, so nobody hears dead air.
3. Silero VAD marks speech start and stop. After `silence_timeout_s` (0.6 s) of
   quiet, the turn is over.
4. The transcript goes to the LLM with the whole conversation so far.
5. The LLM either replies (-> TTS -> the caller) or **calls the transfer tool**.
6. Repeat until someone hangs up, or `idle_timeout_s` (30 s) of total silence.

---

## 6. Transfer

```
LLM: transfer_to_department(department="billing")   engine/pipecat_engine.py
  |  validate against the enum; anything unknown -> "human"
  |  session.can_transfer?  no -> tell the caller the truth, stop here
  |
  +- asyncio.create_task(do_transfer())    <- fire, do NOT await
  +- return "Connecting the caller to billing now."  -> the LLM says it
        |
        |  ... 3 s (transfer_announce_s) so the line actually plays ...
        v
   session.transfer("billing")                  transports/asterisk.py
        v
   POST /channels/<caller>/continue                  ari_controller.py
        { context: "transfer", extension: "billing", priority: 1 }
        v
   Asterisk pulls the channel OUT of Stasis, drops it at [transfer] billing,1
        |
        +-> the external-media leg dies
        |     -> read_audio() returns None -> the pipeline ends
        |     -> engine.run() returns -> finally: release the agent, close audio
        |
        +-> dialplan: Set(GROUP()=transferred) -> Dial(PJSIP/102,30)
                                               -> Goto(after-dial,1)
```

Three things that look wrong and are not:

* **Fired, not awaited.** The tool must return so the LLM can *say* the line. If
  the transfer ran inline, the caller would be moved before hearing anything.
* **The 3 s pause.** Leaving Stasis kills audio instantly; without the delay the
  agent is cut off mid-word and a working transfer feels broken.
* **The department name IS the dialplan extension.** Python never learns a phone
  number; `extensions.conf` decides who "billing" rings.

---

## 7. Capacity — two gates

```
caller  -->  [dialplan]  GROUP_COUNT(agents) >= 3 ?
                 | yes -> Playback(all-circuits-busy-now) -> Hangup   (civil)
                 | no  -> Set(GROUP()=agents) -> Stasis
                             |
                             v
             [app]  pool.acquire() is None ?
                 | yes -> transport.reject() -> immediate hangup      (blunt)
                 | no  -> serve the call
```

The **app-side gate is primary**: transport-agnostic, and the only gate a vendor
without a dialplan has. The dialplan gate exists purely so the caller hears
something civil. `POOL FULL` in the bot log on an Asterisk call therefore means
the two numbers have drifted apart ([[decisions]] 031).

---

## 8. Ending, and cleanup

Any of these ends a call, and all of them converge on the same `finally`:

| Trigger | What happens first |
|---|---|
| Caller hangs up | AudioSocket closes -> `read_audio()` returns `None` |
| Transfer | external-media leg dies -> same as above |
| Idle 30 s | the engine stops the pipeline itself |
| Engine raises | caught and logged in `run_call` |
| Task cancelled | the `finally` still runs |

```
engine.run() returns
      |
      +- pool.release(persona)   -> "released 'Sarah' | 3/3 free"
      +- session.hangup()        -> stop the I/O threads (audio only)

meanwhile, ARI StasisEnd -> _teardown(): destroy the bridge and media channel
```

**The invariant to watch in the logs:** after every call ends, the count returns
to `N/N free`. If it does not, an agent leaked and capacity has silently dropped.

---

## 9. What runs where (the concurrency model)

One process. Inside it:

| | How many | What it does |
|---|---|---|
| asyncio event loop | 1 | everything except socket I/O |
| ARI WebSocket task | 1 | receives Asterisk events, dispatches each as a task |
| accept thread | 1 | blocking `accept()`, dispatches `_intake` per call |
| **read thread** | **1 per call** | blocking `recv()`, parses frames |
| **write thread** | **1 per call** | sends a frame every 20 ms, forever |
| call task | 1 per call | `run_call` -> the engine -> the pipeline |

Three concurrent calls = the loop, 2 service threads, 6 call threads, 3 tasks.

**Why socket I/O is on threads and not the loop.** AudioSocket is lockstep with
zero tolerance: if a frame is not handed over the instant it is due, Asterisk
abandons the call. Any stall on the loop — a model load, GC, LLM inference — was
long enough to do it. Blocking `recv()` releases the GIL, so the socket keeps
draining no matter what the pipeline is doing ([[decisions]] 001).

**The consequence to remember:** if the event loop blocks, the write thread
carries on sending silence and *the logs still look alive*. That is exactly what
[[bugs]] B-011 looked like — 37 s of `0 real` frames, and a 2 s timeout that
reported itself 37 s late.

---

## 10. Where state lives

| State | Scope | Lives in |
|---|---|---|
| Who is free / busy | **whole service** | `AgentPool` — the only shared mutable state |
| Persona (name, voice, prompt) | shared, **frozen** | `PoolPersona`, read-only, never mutated |
| Conversation history | **one call** | that call's engine, discarded at hangup |
| VAD, STT/TTS/LLM connections | **one call** | that call's engine |
| Audio buffers | **one call** | that call's `AudioSocketConnection` |
| ARI channel / bridge ids | **one call** | `AriCall` in the controller's registry |

The rule underneath: **nothing stateful is shared between calls.** That is what
stops two concurrent callers contaminating each other, and what makes a reused
persona start blank for the next caller. It is a privacy property, not a
performance one — caching an engine per persona would break it without failing a
single test.

---

## 11. "I want to change X" — where to look

| Change | File |
|---|---|
| Add/remove an agent, change a voice | `config.yaml` -> `pool.personas` (**and** the dialplan cap) |
| What an agent says / how it behaves | `prompts/<name>.txt` |
| Model, provider, VAD timing | `config.yaml` -> `engine:` |
| Add a transfer department | `engine/pipecat_engine.py` **and** the `[transfer]` context — both |
| Who a department dials | `extensions.conf` only |
| Capacity behaviour | `core/pool.py` + the dialplan gate |
| The call policy (order of acquire/build/release) | `bot.py` `run_call` |
| The pipeline itself | `engine/pipecat_engine.py` |
| Anything Asterisk-specific | `transports/asterisk.py`, `ari_controller.py` |
