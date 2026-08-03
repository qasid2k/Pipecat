# Changelog

Dated, newest first. One entry per phase / notable change. Related:
[[architecture]], [[decisions]], [[bugs]], [[runbook]], [[personas]].

---

## 2026-07-31 — Multi-agent pool, Phase 4: the dialplan capacity gate
The (N+1)th Asterisk caller now hears a spoken busy message before ever reaching
the app, instead of a silent hangup.

* `GROUP_COUNT(agents) >= 3` on extension 6001, checked **before** joining the
  group — joining first would make the caller count themselves and cost a slot.
* A transferred call **frees** its slot (`Set(GROUP()=transferred)` in
  `[transfer]`), so the dialplan measures AI agents in use — the same thing the
  pool measures ([[decisions]] 032). Without it a long human conversation blocks
  a caller from an agent that is free.
* Built-in sounds, not a custom recording: `Playback(all-agents-busy)` names a
  file that does not exist and would fall straight through to `Hangup()` —
  the silent drop this gate exists to remove ([[decisions]] 033).
* The app-side `pool.acquire() is None` check stays as the safety net, and as the
  only gate for a transport with no dialplan ([[decisions]] 031).

The dialplan lives **outside this repo**. The paste-ready snippet, the
slot-release proof for all three exit paths, the N-sync rules and the provider
concurrency caveat are in [[runbook]] §4.

---

## 2026-07-31 — Multi-agent pool, Phases 1–3: N personas, capacity-gated
One agent became a roster. Any caller gets a free agent; simultaneous callers get
different ones; a caller arriving when all N are busy is refused rather than
answered badly.

* **Phase 1** — `pool.personas` in `config.yaml`: Alex, Sarah, Daniel, each with
  their own name, voice and prompt file. Capacity is *derived*
  (`N = len(personas)`), never configured twice ([[decisions]] 026). A persona is
  an override of engine settings, built through the existing `create_engine`
  factory ([[decisions]] 027). The roster is validated at startup — empty list,
  duplicate names, blank voice, unreadable prompt — so a misconfigured service
  refuses to start instead of failing on a real call.
* **Phase 2** — `core/pool.py`: `AgentPool`, pure logic, 16 tests. `acquire()`
  returns `None` when full rather than blocking; `release()` is idempotent and
  never raises ([[decisions]] 028). A mutation check confirms the safety tests
  actually fail against a naive implementation.
* **Phase 3** — the pool wired into `run_call`, and the single-agent path
  **deleted**: one call path, no dead code. Per-call engines give isolation
  between concurrent calls and a clean context for the next caller — a privacy
  property, not an optimisation. 10 more tests cover release on a normal end, an
  engine exception, an engine *construction* failure and a cancelled call.

Live-verified on the VM: transfer still works, concurrent callers hear different
agents, and no agent leaks. See [[personas]] for the roster.

---

## 2026-07-30 — Project complete through Phase 4; Phase 5 deferred
The modular + configurable refactor is done and live-verified through Phase 4.

**Delivered:**
1. **Documentation memory** — this Obsidian vault: architecture, 25 decisions,
   10 bugs, a runbook and this changelog.
2. **Modular** — the telephony vendor sits behind `CallSession` /
   `BaseTransport`, and Pipecat behind `Engine`. Two invariants are
   machine-checked: nothing outside `engine/` imports Pipecat, and `core/`
   imports only `abc`, `typing`, `asyncio`.
3. **Configurable** — `config.yaml` drives transport, providers, model, voice,
   persona and turn-taking, with secrets confined to `.env` and every setting
   validated at startup.

Plus one real bug found and fixed along the way ([[bugs]] B-010), which itself
uncovered three more.

**Not delivered: Phase 5 (Twilio).** Deliberately deferred rather than built
unverified — there was no Twilio account to test against, and the phase existed
to *prove* the abstraction, which unrunnable code cannot do. The design is
worked out and recorded in [[runbook]]; the reasoning is [[decisions]] 025.

**Therefore the honest caveat on goal #2:** the transport abstraction has been
exercised by exactly one vendor, so its shape is an informed design, not a
demonstrated one. The three points most likely to need adjustment when a second
vendor arrives are listed in the runbook.

---

## 2026-07-30 — Phase 4: config file + factories (the agent is now configurable)
See [[decisions]] 021–024 and the full key reference in [[runbook]].

* **`config.yaml`** — vendor, providers, model, voice, persona, turn-taking. The
  shipped values reproduce the previous hardcoded behaviour **exactly**
  (verified, including the system prompt being textually identical).
* **`prompts/alex.txt`** — the system prompt, with `{name}` / `{company}`
  placeholders.
* **`core/config.py`** — typed frozen dataclasses + a validating loader.
  Unknown keys, missing required keys, unsupported providers, out-of-range VAD
  timeouts and unreadable prompt files are all **startup** errors naming the
  exact dotted path. Missing environment variables are reported **all at once**.
* **`factories.py`** — `create_transport(config)` / `create_engine(config)`,
  deliberately outside `core/` so the layering survives.
* `PipecatEngine` builds its STT/LLM/TTS services and turn-taking from config.
  `smart_turn_v3: true` hands turn-taking back to Pipecat's default model.
* `transport_from_env()` removed — `config.yaml` is now the single source of
  truth, and two ways to configure one thing is how a machine ends up running
  settings nobody can find.
* **Secrets rule enforced**: `config.yaml` names env vars and never holds
  values; only 4 secrets remain in `.env`. `.env.example` rewritten to match.
* `config.local.yaml` git-ignored for machine-specific overrides; the config
  file can also be chosen by argument or `$VOICEAGENT_CONFIG`.
* Startup now logs which config file was loaded and what it selected.

**Two behaviour changes worth knowing:**
1. `AUDIOSOCKET_HOST/PORT`, `ARI_BASE_URL`, `ARI_APP` and `TRANSFER_CONTEXT` no
   longer do anything in `.env` — they moved to `config.yaml`.
2. Missing ARI credentials are now a **startup failure**, not a silent downgrade
   to "transfer quietly broken" ([[decisions]] 022).

36/36 Phase 4 checks; Phase 2 (26/26), Phase 3 (19/19) and B-010 (28/28) all
still green — **109 total**. **Needs a live call to confirm.**

---

## 2026-07-30 — Phase 3: Pipecat isolated behind our own Engine interface
**Structural only — no behaviour change intended.** See [[decisions]] 018–020.

* New `core/engine.py`: the `Engine` contract, one method, `run(session)`.
* New `engine/` package — **everything that imports Pipecat now lives here**:
  * `pipecat_engine.py` — `PipecatEngine`: the persona prompt, the STT→LLM→TTS
    pipeline (Deepgram / `gemini-flash-lite-latest` / `aura-2-helena-en`), the
    Silero VAD + 0.6 s stop strategy, the `transfer_to_department` tool, and the
    per-call lifecycle.
  * `session_transport.py` — the Pipecat glue, moved out of the AudioSocket
    module and renamed `CallSession{Input,Output,}Transport`. It was never
    really Asterisk-specific.
  * `transcripts.py` — `TranscriptRecorder` + `save_conversation`.
* `audiosocket_transport.py` → **`transports/audiosocket.py`** (`git mv`, history
  preserved), now just the protocol and its I/O threads.
* `bot.py` is down to ~90 lines of wiring: build a transport, build an engine,
  run calls, guarantee cleanup. It imports no Pipecat and opens no socket.
* **`run_call` owns the session, not the engine** — `hangup()` happens in a
  `finally` so it is guaranteed on every path, including an engine crash, and no
  future engine has to remember it.
* Two invariants are now machine-checked: nothing outside `engine/` imports
  Pipecat, and `core/` imports only `abc`, `typing`, `asyncio`.
* Values are still hardcoded in the engine — Phase 4 moves them to config.yaml.
* 19/19 Phase 3 checks; Phase 2 (26/26) and B-010 (28/28) suites still green.
  Pipeline stages and the transfer tool verified unchanged.
* **Needs a live call to confirm.**

---

## 2026-07-30 — Fix: the ARI addChannel race ([[bugs]] B-010)
Phase 2 passed its live checkpoint (conversation, transfer, the "no one
available" path and two concurrent calls all confirmed on real phones), then this
pre-existing race was fixed on top. See [[decisions]] 017.

* Setup now waits for the media channel's own `StasisStart` before bridging it,
  instead of racing it. 2 s timeout as a safety net.
* ARI event handlers are dispatched as **tasks** — mandatory, or waiting for that
  event deadlocks the WebSocket read loop.
* `_add()`'s result is checked; a real failure tears the call down loudly instead
  of logging "bridged into the AI pipeline" over the top of it.
* `_req()` now distinguishes success-with-no-body (`True`) from failure (`None`).
  Without this the new check would have treated **every** `204 No Content`
  success as a failure and torn down every call.
* Fixed a phantom-call bug that the added concurrency exposed: a caller hanging
  up mid-setup caused our own media channel to be answered as a new incoming
  call. Media-channel ids now retire on `StasisEnd`, not at teardown.
* 28/28 event-ordering checks, plus the Phase 2 suite still at 26/26.
* **Needs one more live call to confirm.**

---

## 2026-07-30 — Phase 2: transport interface + Asterisk adapter
**Structural only — no behaviour change intended.** See [[decisions]] 013–016.

* New `core/transport.py`: the `CallSession` and `BaseTransport` contracts, plus
  the canonical audio-format constants (now defined once, imported by the
  adapter instead of being re-hardcoded).
* New `transports/asterisk.py`: `AsteriskTransport` (listening socket + accept
  thread, ARI controller, UUID correlation, per-call intake tasks) and
  `AsteriskCallSession` (async audio, ARI transfer, audio-only hangup). Also
  covers FreePBX.
* `bot.py` shrank to the conversation: it lost the listening socket,
  `make_listen_socket`, the accept thread, the ARI controller global and the
  correlation code. `handle_call(session)` replaces `handle_call(conn, addr)`.
  The main loop is now `async for session in transport.listen()`.
* `transfer_to_department` calls `session.transfer(dept)` instead of reaching
  into ARI. The 3 s announcement delay stays in the tool handler.
* The Pipecat glue transports now read/write through a `CallSession`, so they
  are no longer Asterisk-specific.
* `AriCall` gained `caller_id`, surfaced as `CallSession.caller_id`.
* Threading, pacing, queue bounds and socket options are **untouched**.
* Verified without a phone, over a real TCP socket, playing the part of
  Asterisk: **26/26 checks** — 20.0 ms average pacing, 50 silence frames/s when
  idle, 320-byte frames, correct UUID correlation, `[transfer] billing,1` on the
  caller channel, no ARI hangup after a transfer, two concurrent calls with no
  audio leakage, and the `None` sentinel on caller hangup. The Pipecat pipeline
  builds on a session with the same stages and the tool handler still wired.
* **Not yet verified: a real phone call.** That is the Phase 2 checkpoint.

---

## 2026-07-30 — Phase 1: docs vault + version alignment
Branch `feature/modular-configurable`, based on `519cd29`.

* **Pipecat aligned to 1.6.0** on the dev laptop (was 1.5.0; the VM was already
  1.6.0) and **pinned** in `requirements.txt`, killing the version drift.
  `aiohttp` added as an explicit requirement — `ari_controller.py` imports it
  directly rather than relying on it arriving via Pipecat.
* Verified on 1.6.0 that every Pipecat API the code uses is unchanged:
  `GoogleLLMService.Settings(model=…)`, `DeepgramTTSService.Settings(voice=…)`,
  `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=…)`,
  `PipelineWorker(idle_timeout_secs=…, app_resources=…)`,
  `FunctionSchema(handler=…)`. `bot.py` and `audiosocket_transport.py` both
  import cleanly.
* Created the `docs-vault/` Obsidian vault: [[architecture]], [[decisions]],
  [[bugs]], [[runbook]], [[changelog]].
* `architecture.md` documents the system **as built**, read off the code — the two
  call paths (6000 direct vs 6001 Stasis), the AudioSocket framing and threading
  model, the ARI/External Media setup, the UUID correlation, and the transfer
  mechanism.
* `decisions.md` seeded with 12 entries, the first 8 recovered from code comments
  and git history so the reasoning behind the tricky parts is no longer only in
  the comments.
* `bugs.md` seeded with 9 fixed bugs, likewise recovered.
* **No agent logic changed.** The only edits to Python were the docstring version
  banners in `bot.py` and `audiosocket_transport.py` (1.5.0 → 1.6.0).

---

## 2026-07-30 — Phase 0: safety branch
* Created `feature/modular-configurable` off `519cd29` with a clean tree.
  `Pre-modular` is left untouched as the known-good fallback; nothing is
  committed to it for the duration of this project.

---

## Before this project (from git history)
* `519cd29` — fix `save_conversation` crash on `LLMSpecificMessage` after a tool
  call ([[bugs]] B-007).
* `5da21ff` — department routing via `transfer_to_department`; demo-stable.
* `d751590` — fix recording filename collision for calls in the same second
  ([[bugs]] B-006).
* `95268db` — fix transfer: prompt the LLM to *call* the tool, not just announce
  it ([[bugs]] B-004).
* `a7b5b84` — add the `transfer_to_human` LLM tool (later generalised to
  departments).
