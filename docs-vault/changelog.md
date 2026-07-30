# Changelog

Dated, newest first. One entry per phase / notable change. Related:
[[architecture]], [[decisions]], [[bugs]], [[runbook]].

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
