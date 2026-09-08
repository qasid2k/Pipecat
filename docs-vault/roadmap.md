# Roadmap

**What is built, what is next, and what was deliberately left out.**

The third list is the point of this note. Scope decisions made in a working
session evaporate when the session ends, and the same "should we add queueing?"
conversation then happens again from scratch. Anything here marked *not now* has
already been considered and set aside on purpose — reopen it against the trigger
written next to it, not from memory.

Related: [[flow]], [[architecture]], [[decisions]], [[changelog]], [[runbook]].

---

## 1. Where we are

A single-process voice agent on the Asterisk VM. A caller dials, one of three
personas answers, converses, and can hand the call to a human department. Three
callers can be served at once; a fourth is told everyone is busy.

| Capability | State |
|---|---|
| Conversation (Deepgram STT → Gemini → Deepgram TTS, 8 kHz) | live |
| ARI call control + AudioSocket media, joined by UUID | live |
| Transfer to sales / support / billing / human | live |
| `DIALSTATUS` "no one available" handling | live |
| Vendor behind `BaseTransport`, Pipecat behind `Engine` | live |
| `config.yaml` drives providers, model, voices, prompts, timing | live |
| 3-persona pool, round-robin, capacity-gated | live |
| Dialplan busy message + slot release | applied; **live results not yet reported** |
| Graceful drain, startup health, adapter contract | **next — see §2** |

Two completed project arcs are recorded in [[changelog]]: the *modular +
configurable* refactor, and the *multi-agent pool*.

---

## 2. Next: Phase 5 — service readiness

The pool works. This phase is about being able to *operate* it and to *extend*
it, and it is the last phase of the current project.

- [x] **Graceful drain** on SIGTERM/SIGINT — stops accepting, refuses callers who
      arrive mid-shutdown, waits `service.drain_timeout_s` (default 30 s), then
      cancels what is left (safe: each `finally` still releases its persona), and
      only *then* tears down the transport. A second Ctrl+C skips the wait.
      `bot.py` `_install_signal_handlers` / `_drain`; 8 tests in
      `tests/test_drain.py`. See [[runbook]] §3.
- [x] **Startup health** — N, persona names, transport and engine logged at boot;
      invalid config still refuses to start.
- [ ] **Resource measurement** — CPU and memory per concurrent call, and a
      *tested* ceiling for N on this VM. Measure, do not estimate. See §4.
- [x] **`adapters.md`** — written: the contract, its three sub-contracts (audio,
      control, capacity), the threading rules, a capability checklist, and an
      honest note that it has been exercised by exactly one vendor.
- [x] **Engine-agnostic audit** — now a committed test rather than a claim.
      `tests/test_layering.py` parses imports with `ast` and enforces that
      nothing outside `engine/` imports Pipecat, that `core/` depends on no
      adapter or engine, and that `bot.py` / `core/pool.py` never reference
      `PipecatEngine` in code.
- [ ] **Structured logging + call records** — moved into Stage B of the call-centre
      plan, where it is a prerequisite rather than a nicety.

---

## 3. Deliberately not built

Each of these was considered and set aside. The trigger column is what should
make us reopen it.

| Item | Why not now | Reopen when |
|---|---|---|
| **Queueing** (hold callers instead of the busy message) | A hard busy is honest and simple; a queue needs hold music, position, timeouts, abandon handling and a whole new failure surface. `acquire()` returning `None` rather than blocking was chosen partly to keep this a clean later addition ([[decisions]] 028). | Callers regularly hit the busy message — i.e. demand genuinely exceeds N and raising N is not the right answer. |
| **Second `Engine` implementation** | The seam is designed and kept clean, but building a second engine with no need for one proves nothing and doubles maintenance. | A real requirement appears — a different LLM stack, or a non-voice channel (chat/web) reusing the same pool and transfer logic. |
| **Twilio transport** | Designed but **not built**, on purpose: there was no Twilio account to verify against, and the phase existed to *prove* the transport abstraction, which unrunnable code cannot do ([[decisions]] 025). Two traps recorded for whoever does build it: do not reuse Pipecat's Twilio serializer (it would make `transports/` import Pipecat), and do not use `audioop` (removed in Python 3.13). | A second carrier is actually needed, or an account exists to test against. |
| **Per-persona metrics / dashboards** | Nothing is measured yet, so there is nothing to dashboard. Logging came first deliberately. | Phase 5's logging is in use and specific questions arise that logs cannot answer. |
| **Call audio recording** | Transcripts are already written per call. Recording *audio* adds storage, retention and consent obligations that have no owner yet. | A compliance or QA requirement makes it necessary — and a retention policy exists first. |
| **Sticky assignment** (a repeat caller gets the same agent) | Would require caller identity, a mapping with a lifetime, and a decision about what happens when their agent is busy. Round-robin ([[decisions]] 034) is the deliberate opposite. | Callers ask for "the person I spoke to before" — which only matters once agents can actually remember, i.e. after caller context exists. |
| **MCP / caller context lookup** | Was in the original project vision and deferred by choice. The agent cannot look anything up today; it says so and transfers. | The agent needs to answer questions about real accounts, orders or bookings. |
| **Horizontal scaling across VMs** | One process on one VM has not been pushed anywhere near its limit, and the pool is in-process memory — sharing it across machines is a genuinely different design (shared state, sticky media, distributed capacity). | The measured ceiling from §2 is actually being reached. |
| **Live config reload** | Config is read once at startup; changing the roster needs a restart, which is acceptable and documented ([[personas]]). | Restarts become disruptive — i.e. calls are frequent enough that dropping in-flight ones matters. |

---

## 4. Open questions and unverified claims

Written down because an unknown that nobody has named tends to be discovered by
a caller.

| # | Question | Why it matters |
|---|---|---|
| 1 | **Provider concurrency limits are unknown.** The table in [[runbook]] §4 is deliberately blank rather than guessed. All personas share one Deepgram key and one Gemini key, so N calls = N concurrent streams on each. | Exceeding them looks exactly like a code bug: some calls answer, others die on connect, and nothing in this repo is at fault. Academic at N=3; real in the tens. |
| 2 | **Are `aura-2-thalia-en` and `aura-2-orion-en` real Deepgram voices?** Never verified against the account. | A typo surfaces as a TTS failure on that persona's first call, not at startup ([[personas]]). |
| 3 | **Phase 4 dialplan — partly confirmed.** The `GROUP` gate is live and working: a call in progress was observed holding `agents`, and the group cleared afterwards. **Still unconfirmed: the spoken busy message, and the transferred and caller-dropped exit paths.** | A slot that is not freed reduces capacity permanently and silently. |
| 4 | **The `released 'Daniel'` line was never confirmed** in the log where his call was torn down while the pipeline was still cancelling. Probably fine; unproven. | If it is not there, an agent leaked. |
| 5 | **No tested ceiling for N**, and no CPU/memory figures per call. | Raising N is currently a guess. Phase 5 item. |
| ~~6~~ | ~~The layering invariants have no committed checker.~~ **Closed 2026-09-08** — `tests/test_layering.py` enforces them on every run ([[decisions]] 036). | — |
| 7 | **The Asterisk configuration exists only on the VM** — dialplan, `ari.conf`, `http.conf`, PJSIP endpoints. Everything else is in git; this is not. | If the VM is lost, the `[transfer]` context and the capacity gate go with it. See §5. |
| 8 | **Each call loads its own Silero VAD model** (~0.4 s, on the event loop). Deliberate — a shared VAD would break per-call isolation — but it stacks: N calls arriving together stack N × ~0.4 s of blocking. | Fine at N=3. Worth revisiting before N grows. See [[bugs]] B-011. |

---

## 5. Documentation gaps

The vault covers architecture, decisions, bugs, operations, personas and flow.
Identified as missing, in the order I would write them:

1. **`asterisk-config.md`** — the complete off-repo Asterisk configuration.
   This is the only part of the system with no backup (§4 #7).
2. **`glossary.md`** — ARI, Stasis, External Media, bridge, context/extension/
   priority, DIALSTATUS, PJSIP, slin, VAD, barge-in, lockstep. The vault assumes
   this vocabulary throughout.
3. **`index.md`** — a stated entry point and reading order. Eight notes now, and
   no signpost.
4. **`testing.md`** — what the 27 tests cover, how to run them, and honestly what
   is **not** tested: audio quality, real provider behaviour, sustained load, and
   the dialplan itself.
5. **`security-privacy.md`** — `recordings/` holds caller transcripts and logs
   hold caller numbers. That is personal data with no stated retention or access
   policy anywhere.

`adapters.md` is not on this list because it is a Phase 5 deliverable (§2).

---

## 6. How to use this note

* Finishing a piece of work → move it into §1 and add a [[changelog]] entry.
* Tempted by something in §3 → check the trigger first. If the trigger has not
  happened, the answer is still no, and that is not a fresh judgement call.
* Answering something in §4 → replace the question with the answer, here and in
  whichever note owns it.
* Reversing a decision → [[decisions]] is **append-only**. Supersede, never edit.
