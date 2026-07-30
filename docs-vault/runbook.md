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
Agent 'Alex' for Techbridge | transport=asterisk | engine=pipecat | deepgram STT -> gemini-flash-lite-latest -> aura-2-helena-en
AudioSocket server listening on 0.0.0.0:8090
Call 6000 (direct) or 6001 (via ARI/Stasis) to talk to it.
Transcripts will be saved to .../recordings
ARI call control ENABLED (app 'voiceagent')
```

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
   transcript files appear; neither call's audio leaks into the other.
6. **Clean end.** The final log line reports duration, `in`/`out`/`out_real`
   frame counts, and a `CAUSE:` — which should be the caller hanging up, not an
   exception.

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

### Add a telephony transport (e.g. Twilio)
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
