# Runbook

How to run it, what it needs, and how to extend it. Current as of **2026-07-30**
(pre-refactor). Related: [[architecture]], [[decisions]], [[bugs]], [[changelog]].

---

## 1. Prerequisites

| Thing | Value |
|---|---|
| Python | 3.12 |
| Pipecat | **1.6.0, pinned** in `requirements.txt` |
| Asterisk | 20.17.0, with ARI enabled and a Stasis app named `voiceagent` |
| Accounts | Deepgram (STT **and** TTS), Google AI Studio (Gemini) |

Both the VM and the dev laptop **must** run the same Pipecat version. See
[[bugs]]/[[decisions]] 012 for why this is called out so loudly.

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux:    source .venv/bin/activate
pip install -r requirements.txt
```

---

## 2. Secrets and configuration

Two files, with a strict split:

* **`.env`** — secrets ONLY. Git-ignored. Copy from `.env.example`.
* **`config.yaml`** — everything else. **Safe to commit**: it names environment
  variables (`api_key_env: DEEPGRAM_API_KEY`) but never holds their values.

`.gitignore` also excludes `.venv/`, `__pycache__/`, `recordings/`, `*.log` and
`config.local.yaml`.

### `.env` — the only secrets

| Variable | Required? | What it does |
|---|---|---|
| `DEEPGRAM_API_KEY` | **yes** | one key serves **both** STT and TTS |
| `GEMINI_API_KEY` | **yes** | the LLM |
| `ARI_USER` | **yes**, unless ARI is disabled | matches `/etc/asterisk/ari.conf` |
| `ARI_PASSWORD` | **yes**, unless ARI is disabled | matches `ari.conf` |

> Settings that used to live here — `AUDIOSOCKET_HOST` / `AUDIOSOCKET_PORT`,
> `ARI_BASE_URL`, `ARI_APP`, `TRANSFER_CONTEXT` — **moved to `config.yaml`**.
> Setting them in `.env` now does nothing.

### Which config file is used
The path given on the command line, else `$VOICEAGENT_CONFIG`, else
`config.yaml` beside the code.

```bash
python bot.py                      # config.yaml
python bot.py config.local.yaml    # explicit (config.local.yaml is gitignored)
VOICEAGENT_CONFIG=/etc/agent.yaml python bot.py
```

### `config.yaml` — `transport`

| Key | Default | What it does |
|---|---|---|
| `provider` | `asterisk` | Which vendor. Valid: `asterisk` (covers FreePBX). |
| `asterisk.ari_url` | `http://localhost:8088` | ARI HTTP endpoint; matches `http.conf`. |
| `asterisk.ari_app` | `voiceagent` | Must match `Stasis(...)` in the dialplan. |
| `asterisk.ari_user_env` | `ARI_USER` | **Name** of the env var holding the ARI user. |
| `asterisk.ari_pass_env` | `ARI_PASSWORD` | **Name** of the env var holding the password. **Comment it out to run deliberately without call control** — transfer then stops working. |
| `asterisk.audiosocket_host` | `0.0.0.0` | Address our AudioSocket server binds. |
| `asterisk.audiosocket_port` | `8090` | Port it binds. |
| `asterisk.media_host` | `127.0.0.1` | Address ARI tells Asterisk to dial for media. Loopback, because the bot runs on the VM. |
| `asterisk.transfer_context` | `transfer` | Dialplan context a transfer lands in. |

### `config.yaml` — `engine`

| Key | Default | What it does |
|---|---|---|
| `provider` | `pipecat` | Which conversation engine. Valid: `pipecat`. |
| `stt.provider` | `deepgram` | Valid: `deepgram`. |
| `stt.api_key_env` | `DEEPGRAM_API_KEY` | Name of the env var with the key. |
| `stt.sample_rate` | `8000` | Telephony native; changing it breaks the no-resampling promise ([[decisions]] 003). |
| `stt.model` | *(unset)* | Optional Deepgram model override. |
| `llm.provider` | `google` | Valid: `google`. |
| `llm.model` | `gemini-flash-lite-latest` | Read [[decisions]] 004 first — latency matters more than IQ on a phone call. |
| `llm.api_key_env` | `GEMINI_API_KEY` | |
| `tts.provider` | `deepgram` | Valid: `deepgram`. |
| `tts.voice` | `aura-2-helena-en` | Any Deepgram Aura-2 voice. |
| `tts.api_key_env` | `DEEPGRAM_API_KEY` | The same key as STT. |
| `tts.sample_rate` | `8000` | |
| `turn_taking.vad` | `silero` | Valid: `silero`. |
| `turn_taking.silence_timeout_s` | `0.6` | Silence after speech before the turn ends. Must be 0.05–10; ~0.3–2.0 is sane. |
| `turn_taking.smart_turn_v3` | `false` | `true` hands turn-taking back to Pipecat's default ONNX model ([[decisions]] 005). |
| `persona.name` | `Alex` | Substituted for `{name}` in the prompt file. |
| `persona.company` | `Techbridge` | Substituted for `{company}`. |
| `persona.system_prompt_file` | `prompts/alex.txt` | Path relative to config.yaml. |
| `persona.system_prompt` | — | Inline alternative. Setting **both** is an error. |
| `persona.greeting` | derived from name + company | Spoken immediately on answer. |
| `idle_timeout_s` | `30` | End the call after this much total silence. |
| `transfer_announce_s` | `3.0` | How long "connecting you now" gets to play before the transfer ([[decisions]] 008). |

### What happens when it's wrong
Bad config is a **startup** error naming the exact dotted path — never a
surprise mid-call. Caught: unknown keys (so `voicce:` is an error, not a
silently ignored typo), missing required keys, unsupported provider names (the
message lists the valid ones), out-of-range VAD timeouts, an unreadable prompt
file, and **every** missing environment variable reported in one go rather than
one per restart.

Still in code, not config: the transfer **department list**
(`engine/pipecat_engine.py`). It must stay in step with the `[transfer]`
dialplan, so changing it is a two-sided change that deserves a review rather
than a config tweak.

### Where things live
```
config.yaml                WHAT to run   <- most changes land here now
prompts/alex.txt           the system prompt
core/config.py             the typed, validating loader
core/transport.py          CallSession + BaseTransport contracts
core/engine.py             Engine contract
factories.py               config -> real objects
transports/asterisk.py     Asterisk/FreePBX: ARI, bridge, correlation, transfer
transports/audiosocket.py  AudioSocket protocol + I/O threads
ari_controller.py          ARI REST/WebSocket client
engine/pipecat_engine.py   persona, pipeline, tools  <- most edits land here
engine/session_transport.py  CallSession -> Pipecat glue
engine/transcripts.py      transcript recording
bot.py                     wiring only
```

---

## 3. Running it

```bash
python bot.py
```

Expect on startup — the second line is worth reading, because it tells you
exactly which config took effect:

```
Config: /path/to/config.yaml
transport=asterisk | engine=pipecat | deepgram STT -> gemini-flash-lite-latest -> deepgram TTS
Pool: capacity 3 (max simultaneous calls)
  - Alex (aura-2-helena-en)
  - Sarah (aura-2-thalia-en)
  - Daniel (aura-2-orion-en)
AudioSocket server listening on 0.0.0.0:8090
Call 6000 (direct) or 6001 (via ARI/Stasis) to talk to it.
Transcripts will be saved to .../recordings
ARI call control ENABLED (app 'voiceagent')
```

**`Pool: capacity N` is the number that must match the Asterisk dialplan's
`GROUP` cap** (Phase 4). The dialplan cannot read `config.yaml`, so these two are
kept equal by hand — see [[personas]].

If it exits immediately with `Configuration problem:`, read the message — it
names the exact setting or environment variable at fault.

**Where to run it.** `transport.asterisk.media_host` defaults to `127.0.0.1`, so
**extension 6001 and therefore transfer only work when the bot runs on the
Asterisk VM.** The direct path (6000) can run on the dev laptop, because there
the *dialplan* names the host — during development that was
`192.168.100.67:8090`, i.e. Asterisk dials out to the laptop.

**On a dev laptop without ARI credentials** the bot now refuses to start, rather
than silently running with transfer broken. Either put the ARI variables in
`.env`, or copy `config.yaml` to `config.local.yaml`, comment out `ari_pass_env`
there, and run `python bot.py config.local.yaml`.

Stop with `Ctrl+C`.

---

## 4. Asterisk side

Two extensions, two behaviours:

* **6000 — direct.** `AudioSocket(<uuid>,<host>:8090)`. Conversation works;
  **transfer does not** (no ARI channel to act on).
* **6001 — Stasis.** `Stasis(voiceagent)`. The real path: transfer-capable.

A `[transfer]` context is also required, with **one extension per department** —
`sales`, `support`, `billing`, `human`. Each `Dial()`s the endpoint for that
team. The department name chosen by the LLM *is* the extension name, so a missing
entry means a failed transfer.

`DIALSTATUS` branching lives here too: on `BUSY` / `NOANSWER` / `CHANUNAVAIL`,
play the "no one available" message rather than dropping the caller silently.
**This is part of the product's behaviour and lives outside this repo** — keep it
backed up and re-test it after every refactor phase.

FreePBX is Asterisk underneath: same ARI, same Stasis, same AudioSocket. Only the
authoring route differs (custom contexts via the FreePBX UI). No separate adapter.

### The capacity gate (dialplan side)

There are **two** capacity gates and they do different jobs:

| Gate | Where | Caller hears | Applies to |
|---|---|---|---|
| `GROUP_COUNT` cap | dialplan, before the app | a spoken busy message | Asterisk only |
| `pool.acquire() is None` | `bot.run_call` | an immediate clean hangup | **every** transport |

The **app-side gate is primary** — it is transport-agnostic and is the only gate a
vendor without a dialplan has. The dialplan gate exists purely so the caller hears
something civil instead of a dead line. Never delete the app-side one; it is the
safety net for exactly the case below where the two numbers drift.

Add the gate to the **6001** extension. `N` here is **3** — it must equal
`Pool: capacity N` from the bot's startup banner:

```
exten => 6001,1,NoOp(AI agent call from ${CALLERID(num)})
 ; Count BEFORE joining the group. Joining first would make the caller count
 ; themselves, so the Nth caller would see N and be rejected -- an off-by-one
 ; that silently costs one slot of real capacity.
 same => n,GotoIf($[${GROUP_COUNT(agents)} >= 3]?busy)
 same => n,Set(GROUP()=agents)
 same => n,Stasis(voiceagent)              ; <-- the existing routing line
 same => n,Hangup()

 same => n(busy),NoOp(POOL FULL: ${GROUP_COUNT(agents)} of 3 agents busy)
 same => n,Answer()
 same => n,Wait(1)                          ; let the audio path settle
 ; Built-in Asterisk sounds, so this works with nothing to install. To brand it,
 ; record an 8 kHz mono file into /var/lib/asterisk/sounds/custom/ and replace
 ; these two lines with: same => n,Playback(custom/all-agents-busy)
 same => n,Playback(all-circuits-busy-now)
 same => n,Playback(pls-try-call-later)
 same => n,Hangup()
```

Confirm both sound files exist before trusting the gate — a missing file logs
`file does not exist` and falls straight through to `Hangup()`, which is the
silent drop this gate was added to prevent:

```bash
ls /var/lib/asterisk/sounds/en/ | grep -E 'all-circuits-busy-now|pls-try-call-later'
```

### Freeing the slot on transfer

A channel's `GROUP` is released automatically when that **channel** hangs up. A
transferred call is a problem: ARI `continue`s the *same* channel into
`[transfer]`, so it keeps its `agents` membership for the entire human
conversation — even though the bot released the AI persona the moment it
transferred. Left alone, a 20-minute human call blocks a slot whose agent is
free, and the dialplan cap silently measures something different from the pool.

Fix: move the channel to another group in each of the four department extensions
in `[transfer]`, **before** the `Dial()` — `Dial()` blocks for the whole human
conversation, so anything after it (including the shared `after-dial` handler)
runs far too late:

```
[transfer]
exten => sales,1,NoOp(Transfer to SALES)
 same => n,Set(GROUP()=transferred)
 same => n,Dial(PJSIP/101,30)
 same => n,Goto(after-dial,1)

exten => support,1,NoOp(Transfer to SUPPORT)
 same => n,Set(GROUP()=transferred)
 same => n,Dial(PJSIP/102,30)
 same => n,Goto(after-dial,1)

exten => billing,1,NoOp(Transfer to BILLING)
 same => n,Set(GROUP()=transferred)
 same => n,Dial(PJSIP/102,30)
 same => n,Goto(after-dial,1)

exten => human,1,NoOp(Transfer to OPERATOR)
 same => n,Set(GROUP()=transferred)
 same => n,Dial(PJSIP/102,30)
 same => n,Goto(after-dial,1)
```

**Any department added later needs this line too.** Miss one and that department
leaks a slot for the length of every human call — a failure that surfaces weeks
later as "we can only take two calls now."

Note that `support`, `billing` and `human` all dial the same endpoint today, so
routing cannot be verified by which handset rings. Check the `NoOp` line in the
Asterisk log, or `TOOL: transfer_to_department -> billing` in the bot's.

Reassigning is used rather than clearing (`Set(GROUP()=)`) because a channel holds
one group per category, so naming a different group unambiguously removes it from
`agents` — and `GROUP_COUNT(transferred)` then tells you how many callers are with
a human, which is worth knowing anyway.

### Proving the slot is freed on every exit path

Capacity that leaks is worse than capacity that is too low: it degrades silently,
one call at a time, until the service is answering one caller. After **each** of
the three paths below, the count must return to `0`:

```bash
asterisk -rx "group show channels"       # which channels are in which group
asterisk -rx "dialplan show 6001@<your-context>"   # confirm the gate is live
```

| Path | How to trigger | Expected |
|---|---|---|
| Normal hangup | talk, then hang up | `agents` count back to 0 |
| Transferred call | ask for billing, hang up after the human | `agents` drops to 0 **at transfer**, `transferred` drops to 0 at hangup |
| Caller dropped | hang up mid-sentence | `agents` count back to 0 |

If a count sticks above 0 with no live call, that slot is gone until Asterisk
restarts. Cross-check against the bot's own `released ... | 3/3 free` line — if
the bot says 3/3 free but `group show channels` still lists `agents`, the leak is
dialplan-side, not app-side.

### Keeping N in sync

`N` exists in two places that **cannot** see each other:

1. `pool.personas` in `config.yaml` — the app's capacity.
2. the `>= 3` in the dialplan above — the spoken-message threshold.

Change the roster and you must change the dialplan number in the same sitting.
The failure is asymmetric, which is why it is worth care:

* **dialplan cap too HIGH** — extra callers get past it, reach the app, and are
  refused by `pool.acquire()` with a silent hangup. Symptom: `POOL FULL` lines in
  the bot log. Annoying, not dangerous — the app-side gate holds.
* **dialplan cap too LOW** — callers hear "all agents busy" while agents sit
  idle. No error anywhere. This one is invisible; only the two numbers disagreeing
  will tell you.

`Pool: capacity N` is printed at every startup precisely so this is checkable in
one glance. See also [[personas]].

### Provider concurrency — check before raising N

All N personas share **one** Deepgram key (STT *and* TTS) and **one** Gemini key.
N concurrent calls therefore means N concurrent Deepgram streams plus N Gemini
request streams. Exceed the account's limit and the provider starts rejecting
connections — which looks exactly like a code bug: some calls answer, others die
on connect, and nothing in this repo is at fault.

**These limits have not been verified for this project's accounts.** Before
raising N, read them off the consoles and record them here:

| Provider | Limit that matters | This account's value | Checked on |
|---|---|---|---|
| Deepgram | concurrent streaming connections (STT + TTS share it) | _fill in_ | _date_ |
| Gemini | requests/min and concurrent streams for the chosen model | _fill in_ | _date_ |

N = 3 is far below any plausible limit, so today this is a note for later, not a
risk. It becomes real somewhere in the tens.

### Changing the roster requires a restart

`config.yaml` is read once, at startup. Adding, removing or re-voicing a persona
takes effect on the **next start** of `bot.py` — there is no live reload, and
none is planned. Restarting drops any call in progress, so do it deliberately.

---

## 5. Verifying a call (the checkpoint test)

Run these after **every** phase — this is the regression suite:

1. **Answer.** Dial 6001. The greeting plays with no leading silence.
2. **Converse.** Ask something. Logs show `VAD: caller started speaking`,
   `VAD: caller stopped speaking`, then `CALLER: <text>` and a spoken reply.
3. **Transfer.** Ask for a human / billing / sales. Logs show
   `TOOL: transfer_to_department -> <dept>` then
   `ARI: transferring <chan> -> transfer,<dept>,1`, and the phone rings.
4. **"No one available".** Transfer to a department whose endpoint is offline.
   The caller must hear the message, not silence.
5. **Two simultaneous calls.** Both converse independently; two sets of
   transcript files appear; neither call's audio leaks into the other. Each
   caller must hear a **different agent — different name AND different voice**.
6. **Clean end.** The final log line reports duration, `in`/`out`/`out_real`
   frame counts, and a `CAUSE:` — which should be the caller hanging up, not an
   exception.
7. **Capacity.** Fill all N slots, then dial once more. With the dialplan gate
   in place the extra caller hears the busy message and the call ends — and
   **nothing appears in the bot log**, because that caller never reached the app.
   A `POOL FULL -- rejecting call` line here means the dialplan cap and the
   roster have drifted; the app-side net caught it. See §4.
8. **Release and reuse.** Hang up, then dial again. The freed agent is handed
   out first (see [[decisions]] 029), so you should get the **same** agent — who
   must remember **nothing** of the previous call. Ask "what did I just tell
   you?"; any recollection is a privacy failure, not a quirk.
9. **No leak.** After every call has ended, the last `released` line must read
   `N/N free (on calls: none)`. A count that never returns to N means an agent
   leaked and capacity has permanently dropped.

### The pool's own log lines

Every line is keyed by the call UUID, so a single call can be followed end to end:

```
[<uuid>] assigned 'Alex' (aura-2-helena-en) to <caller> | 2/3 free (on calls: Alex)
[<uuid>] released 'Alex' (caller hung up) | 3/3 free (on calls: none)
[<uuid>] POOL FULL -- rejecting call from <caller> | 0/3 free (on calls: Alex, Sarah, Daniel)
```

`POOL FULL` on an **Asterisk** call means the dialplan cap and the roster have
drifted apart — the dialplan should have caught that caller first and played the
busy message. Check `Pool: capacity N` at startup against the dialplan's cap.

### Automated tests (no phone needed)

```bash
python -m unittest discover -s tests -t . -v      # 26 tests
```

Covers the pool logic and the call-loop wiring: distinct personas for concurrent
calls, one engine per call, rejection at capacity, and the agent returning to the
pool on a normal end, an engine exception, an engine *construction* failure and a
cancelled call. These do **not** touch audio, Asterisk or the providers — they are
a fast pre-flight, not a substitute for steps 1–9.

Artifacts land in `recordings/`:
`<stamp>-transcript.jsonl` (live, per caller utterance) and
`<stamp>-conversation.json` (both sides, written at hangup), where
`stamp = YYYYmmdd-HHMMSS-<6 hex>`.

---

## 6. Troubleshooting

| Symptom | Look at |
|---|---|
| Call drops after a few seconds; "Resource temporarily unavailable" | [[bugs]] B-001. Never move socket I/O onto the event loop; `SO_RCVBUF` must be set before `bind()`. |
| Agent sounds slow / choppy on Windows | [[bugs]] B-002 (`timeBeginPeriod(1)`). |
| Agent says it will transfer but nothing happens | [[bugs]] B-004 — a prompt problem. Check whether `TOOL: transfer_to_department` appears in the log at all. |
| Transfer works but the caller is cut off mid-sentence | [[bugs]] B-005 — the 3 s pre-transfer delay. |
| Transfer does nothing on extension 6000 | Expected. 6000 has no ARI channel; use 6001. |
| Changed a setting and nothing happened | Check the `Config:` line at startup — you may be editing a different file from the one being loaded (`$VOICEAGENT_CONFIG`, or an argument). A misspelled key is a startup error, so it cannot be the cause. |
| `Configuration problem:` at startup | Read it; it names the dotted path or the env var. This is deliberate — it fails before answering a call rather than during one. |
| `no UUID within 2s; treating as a non-ARI call` | The AudioSocket connection was not correlated to a channel: ARI down, or the External Media `data` UUID did not arrive. Transfer will be unavailable on that call. |
| Nothing transcribed; VAD lines never appear | No caller audio is reaching the pipeline. Check the `in=` counter in the final log line. |
| Call ends after exactly 30 s | `idle_timeout_secs=30` — total silence. The audio path is one-way or dead. |
| Gemini 404s | [[bugs]] B-008 — use a `-latest` alias. |

Frame counters in the closing log line are the fastest diagnostic:
`in` = caller frames received, `out` = frames sent (including silence),
`out_real` = frames of actual agent speech. `out_real == 0` means the agent never
spoke; `in == 0` means we never heard the caller.

---

## 7. How to extend it

> Phases 2–5 of the current project are what make the first two of these true.
> Until then, "adding a provider" still means editing `bot.py`.

### Add / swap an STT, LLM or TTS provider
Pipecat already abstracts providers, so we add **no** wrapper classes of our own
([[decisions]] 009).
1. Install the extra: `pipecat-ai[<provider>]` — and add it to the pin in
   `requirements.txt`.
2. Today: change the one line in `PipecatEngine._build_pipeline()`
   (`engine/pipecat_engine.py`). From Phase 4: change `engine.stt/llm/tts` in
   `config.yaml`.
3. Put the key in `.env` and reference it from config by **name**
   (`api_key_env:`), never by value ([[decisions]] 011).
4. **Confirm the provider accepts 8 kHz.** If it does not, we would have to
   resample, which breaks [[decisions]] 003 — treat that as a design decision
   needing a new entry, not a quick fix.

### Add a telephony transport (e.g. Twilio) — NOT BUILT YET
> **Status: deferred, 2026-07-30.** Designed but deliberately not implemented,
> because there was no Twilio account to verify it against and unverified code
> was worth less than an honest gap. The design notes below are the result of
> the Phase 5 work; see [[decisions]] 025 for the reasoning.

Copy `transports/asterisk.py` as the worked example; the contracts it implements
are documented in `core/transport.py`.
1. Add `transports/<vendor>.py` implementing `BaseTransport` (`start`, `stop`,
   `listen`, `reject`) and `CallSession` (`read_audio`, `write_audio`,
   `transfer`, `hangup`, `can_transfer`, `ended`/`end_reason`). Python will
   refuse to instantiate the class until every abstract method exists.
2. Do per-call setup in a **task**, not inline in `listen()`, or call N+1 waits
   behind call N ([[decisions]] 015).
3. Make `hangup()` release only your audio path — never destroy a call that may
   already have been transferred away ([[decisions]] 014).
4. **Convert audio to/from canonical 8 kHz slin inside the adapter** (the
   constants are in `core/transport.py`). The core must never see mu-law,
   base64, or vendor frames.
5. Implement `transfer()` the vendor's way (Twilio: REST redirect / TwiML). There
   is no dialplan, so the "no one available" fallback becomes app-side.
6. Register it in `create_transport()` and select it with
   `transport.provider:` in `config.yaml` — **no core code change**.
7. Leave the Asterisk path untouched, and re-run §5 against Asterisk to prove it.

#### Twilio specifics, worked out in advance

* **Audio arrives over a WebSocket we host**, not a socket the vendor listens on.
  Twilio connects after a TwiML `<Connect><Stream url="wss://…"/>`, then sends
  JSON events: `connected`, `start` (carries `streamSid` and `callSid` — the
  handles everything else needs), `media` (base64 mu-law), `stop`. We already
  depend on `aiohttp`, whose `web.WebSocketResponse` can host this with no new
  dependency.
* **Convert mu-law → canonical 8 kHz slin inside the adapter**, and buffer to
  exactly 320-byte frames. **Do not import Pipecat's Twilio serializer** to do
  it — that would make `transports/` depend on the engine and undo Phase 3
  ([[decisions]] 025). `audioop` is not an option either: deprecated, and
  **removed in Python 3.13**. G.711 is ~40 lines with two lookup tables, and can
  be verified bit-exact against `audioop` on 3.12 as a test oracle.
* **`write_audio()` must pace itself.** This is the subtle one. Asterisk gets
  pacing for free from the bounded outgoing queue; Twilio accepts everything you
  send it and buffers, so an unpaced adapter would dump a whole TTS utterance in
  one go. The audio would still *play*, but Pipecat's idea of when the bot is
  speaking would be wrong, which breaks barge-in. Pace off a monotonic clock at
  20 ms/frame, the same discipline as the AudioSocket write thread.
* **`transfer()` is a REST call**, not a dialplan hand-off: update the live call
  with TwiML `<Dial>` to the department's number. Twilio has no dialplan, so
  **the department → number mapping becomes config**, and the DIALSTATUS "no one
  available" behaviour has to be rebuilt app-side with a `<Dial action="…">`
  callback pointing at an endpoint we serve.
* **`reject()` finally earns its place.** On Asterisk, capacity is the dialplan's
  problem; on Twilio there is nowhere else to put it, so the adapter must answer
  and say something itself.
* **Only the selected provider's config is validated**, so adding a `twilio:`
  block does not force Asterisk users to hold Twilio credentials, and vice versa.

### Change the persona
Today: edit the `SYSTEM_PROMPT` block in `engine/pipecat_engine.py`. From
Phase 4: `engine.persona.system_prompt_file`. Keep the phone-specific rules — no
markdown, one or two sentences, spoken numbers, one question at a time — they are
what make it bearable to listen to.

### Swap the conversation engine entirely (replacing Pipecat)
1. Add a module under `engine/` implementing `core.engine.Engine` — one method,
   `async run(session)`.
2. Drive audio with `session.read_audio()` / `session.write_audio()`, and
   transfer with `session.transfer(dept)`.
3. **Do not call `session.hangup()`** — `bot.run_call` owns that
   ([[decisions]] 019).
4. Point `create_engine()` at it. Nothing else in the codebase changes; that is
   the whole point of the interface.

### Add a transfer department
1. Add the name to `TRANSFER_DEPARTMENTS` in `engine/pipecat_engine.py` — it
   feeds both the tool enum and the validation.
2. Describe it in the tool's `department` description so the LLM can choose it.
3. **Add the matching `<name>,1,...` entry to the `[transfer]` dialplan context**,
   including its `DIALSTATUS` branch. Skipping this is a silent failure.
