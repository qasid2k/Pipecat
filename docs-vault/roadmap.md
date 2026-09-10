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

## 2. The call-centre stages

Target: **500+ concurrent, elastic**, fully tracked (call records, live
supervisor dashboard, audio recording, analytics + QA), inbound only,
single-tenant now but tenant-seamed for SaaS later.

**500 concurrent is not a bigger version of this process — it is 10–25 nodes.**
The single-node ceiling has never been measured, and what breaks first is not
the pool. In the order it will actually bite:

| # | Limit | Where | Bites around |
|---|---|---|---|
| ~~1~~ | ~~Default `ThreadPoolExecutor`~~ — **FIXED 2026-09-10.** Every 20 ms frame used to block a pool worker; `queue_output` now waits on the event loop instead. Back-pressure preserved and pinned by 7 tests. | `transports/audiosocket.py` | — |
| 2 | **2 OS threads per call**, one waking 50×/s | `transports/audiosocket.py` | tens |
| ~~3~~ | ~~Per-call Silero VAD load~~ — **REDUCED 2026-09-10.** Measured at **170 ms** warm (not the ~0.4 s claimed here; that was the cold first load). Now built via `asyncio.to_thread`: worst loop stall for 8 concurrent builds went **1398 ms → 426 ms**. Not eliminated — onnxruntime holds the GIL for part of session creation. | `engine/pipecat_engine.py` | still a burst-arrival cost |
| 4 | **Provider quotas** — one Deepgram key, one Gemini key, limits unknown | shared credentials | unknown — could be first |
| 5 | **One event loop** — a stall drops calls; it has twice | whole process | any time |

Limits 1 and 3 are cheap fixes that likely move the ceiling several times over,
so they come *before* concluding a node "only" handles N.

- [x] **A — Operability.** Graceful drain on SIGTERM/SIGINT (refuse → wait
      `service.drain_timeout_s` → cancel → *then* stop the transport; a second
      Ctrl+C forces). Startup health. `adapters.md`. The layering invariants made
      a committed test instead of a claim. Found and fixed [[bugs]] B-012 on the
      way. [[decisions]] 035–036.
- [x] **B — Make one call observable.** No new infrastructure; everything below
      consumes it. `core/logging.py` (console + rotating JSON sink, `call_id`
      bound through `contextualize`), Asterisk `uniqueid`/`linkedid` captured and
      exposed via `CallSession.vendor_ids`, recordings keyed by `call_id` with a
      self-describing header, transcript writes moved **off the event loop**,
      `frames_dropped` and the new `pacer_slips` surfaced, `tenant_id` threaded
      through. [[decisions]] 037.
- [x] **C — Persist.** `calls` + `turns`, written through a bounded queue and a
      single writer task, never on the loop. A store outage drops rows with a
      warning and the call carries on. **SQLite, not Postgres** — there was no
      instance to verify against, and unverifiable persistence in a live service
      is what [[decisions]] 025 exists to prevent; the store is behind an
      interface so Postgres is a sibling file, not a rewrite ([[decisions]] 039,
      040). `Engine.run()` now returns an `EngineResult` so `run_call` can write
      the row with the three facts only the engine sees. **Live-verified
      2026-09-10.**
- [x] **D — Control plane + dashboard.** `/health`, `/pool`, `/calls`,
      `/metrics`, `WS /live` and a supervisor page at `/`, over `aiohttp`,
      in-process and read-only, bound to loopback because `/calls` returns caller
      numbers and there is no auth ([[decisions]] 043). Live call state lives in
      `core/live.py`, deliberately separate from the pool ([[decisions]] 044).
      The socket pushes only on change, so an idle service sends nothing
      ([[decisions]] 045). **Live-verified 2026-09-10.**

**Stages A–D are live on the VM as of 2026-09-10** — the service runs, answers,
records, and reports. What that confirms is the *path*: calls are served, rows
are written, the dashboard shows them. It does not by itself confirm the
fine-grained items still listed in §4 below, which need looking at specifically
rather than in passing.
- [ ] **E — Measure the ceiling.** *Gates F.* **Limits 1 and 3 fixed and
      measured** ([[decisions]] 046). Still to do: the load harness, the ramp to
      N_max, CPU/memory per call, and the provider limits in [[runbook]] §4.
      One residual worth knowing before designing the harness: 8 VAD builds at
      once still stall the loop ~426 ms, so **burst arrival is the shape of the
      problem, not sustained load** — the harness must ramp *and* spike.
- [ ] **F — Horizontal scale.** `AgentPool` keeps its interface and gains a Redis
      backing store; the existing pool tests become the contract. Call
      distribution and media routing decided from measured numbers, not now.
- [ ] **G — Recording, analytics, QA.** **Audio recording is gated on privacy
      work, not engineering** — retention, access policy, lawful basis and caller
      notification must exist first (§5).

**Deferred out of Stage B, with reason:** live *agent* turns in the .jsonl. The
recorder sits between STT and the LLM, so it never sees the LLM's output frames;
moving it depends on whether Pipecat's aggregators forward `TranscriptionFrame`
downstream, which cannot be settled without a live pipeline. Both sides are
already captured in `conversation.json`. It belongs in Stage C, where turns go to
the database with real per-turn timings and can be verified on a real call.

---

## 3. Deliberately not built

Each of these was considered and set aside. The trigger column is what should
make us reopen it.

| Item | Why not now | Reopen when |
|---|---|---|
| **Queueing** (hold callers instead of the busy message) | A hard busy is honest and simple; a queue needs hold music, position, timeouts, abandon handling and a whole new failure surface. `acquire()` returning `None` rather than blocking was chosen partly to keep this a clean later addition ([[decisions]] 028). | Callers regularly hit the busy message. **Measure it from CDR, not from our records** — a caller rejected by the dialplan gate never reaches the app, so we have no row for them ([[decisions]] 041): `grep Playback /var/log/asterisk/cdr-csv/Master.csv \| grep -c busy` |
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
| ~~2~~ | ~~Are `aura-2-thalia-en` and `aura-2-orion-en` real Deepgram voices?~~ **Closed 2026-09-10** — the roster has been rotating through all three on live calls, so all three voices resolve. | — |
| 2b | **Is `linkedid` actually populated in the records?** The ARI variable fetch works, but nobody has read the column. | Empty `linkedid` means transferred calls cannot be stitched back together in the CDR — recoverable only while the channel exists. |
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
