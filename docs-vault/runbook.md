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

Copy `.env.example` to `.env` and fill it in. **`.env` is git-ignored — never
commit real keys.** (`.gitignore` also excludes `.venv/`, `__pycache__/`,
`recordings/` and `*.log`.)

| Variable | Required? | Default | What it does |
|---|---|---|---|
| `DEEPGRAM_API_KEY` | **yes** | — | one key serves **both** STT and TTS |
| `GEMINI_API_KEY` | **yes** | — | the LLM |
| `AUDIOSOCKET_HOST` | no | `0.0.0.0` | address the AudioSocket server binds |
| `AUDIOSOCKET_PORT` | no | `8090` | port; also handed to ARI as the media port |
| `ARI_BASE_URL` | no | `http://localhost:8088` | Asterisk ARI HTTP endpoint |
| `ARI_APP` | no | `voiceagent` | must match `Stasis(...)` in the dialplan |
| `ARI_USER` | no | `voiceagent` | must match `/etc/asterisk/ari.conf` |
| `ARI_PASSWORD` | **for transfer** | — | **ARI only starts if this is set** |
| `TRANSFER_CONTEXT` | no | `transfer` | dialplan context transfers land in |

`check_keys()` exits immediately if a Deepgram or Gemini key is missing. A
missing `ARI_PASSWORD` is **not** an error — the bot silently runs without call
control, so transfer stops working. If transfer "just stopped", check this first.

Not yet in the environment (must be edited in `bot.py` until Phase 4):
persona/prompt, greeting, agent name, company name, LLM model, TTS voice, VAD
timeout, department list.

---

## 3. Running it

```bash
python bot.py
```

Expect on startup:

```
AudioSocket server listening on 0.0.0.0:8090
Call 6000 (direct) or 6001 (via ARI/Stasis) to talk to it.
Transcripts will be saved to .../recordings
ARI call control ENABLED (app 'voiceagent')
```

If the last two lines say ARI is not configured, `ARI_PASSWORD` is unset.

**Where to run it.** The ARI path hardcodes the media host as `127.0.0.1`
(`bot.py:506`), so **extension 6001 and therefore transfer only work when the bot
runs on the Asterisk VM.** The direct path (6000) can run on the dev laptop,
because there the *dialplan* names the host — during development that was
`192.168.100.67:8090`, i.e. Asterisk dials out to the laptop.

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
2. Today: change the one line in `build_pipeline()` (`bot.py:263`).
   From Phase 4: change `engine.stt/llm/tts` in `config.yaml`.
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
Today: edit the `SYSTEM_PROMPT` block at `bot.py:87`. From Phase 4:
`engine.persona.system_prompt_file`. Keep the phone-specific rules — no markdown,
one or two sentences, spoken numbers, one question at a time — they are what make
it bearable to listen to.

### Add a transfer department
1. Add the name to `TRANSFER_DEPARTMENTS` (`bot.py:143`) — it feeds both the tool
   enum and the validation.
2. Describe it in the tool's `department` description so the LLM can choose it.
3. **Add the matching `<name>,1,...` entry to the `[transfer]` dialplan context**,
   including its `DIALSTATUS` branch. Skipping this is a silent failure.
