# Changelog

Dated, newest first. One entry per phase / notable change. Related:
[[architecture]], [[decisions]], [[bugs]], [[runbook]].

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
