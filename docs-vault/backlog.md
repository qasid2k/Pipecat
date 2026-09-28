# Improvement backlog

base: feature/multi-agent-pool

**The work list of the improvement loop ([[decisions]] 051, 053).** `/improve`
reads this file and [[product]], adds proposals here, and builds only items
marked `APPROVED`, **one at a time, on the working branch above**. It won't start
the next one until you've tested the last one live.
Related: [[roadmap]], [[improvement-log]], [[runbook]] §8.

> `base:` is the one branch everything is committed to and the VM pulls from.
> Change it if the working branch changes (e.g. to `main`).

---

## How to use it (your part of the loop)

| You want to… | Do this |
|---|---|
| accept an idea | change `status: PROPOSED` to `status: APPROVED` (or tell Claude "approve IMP-…") |
| turn one down | set `status: REJECTED` and fill in `why rejected:`. **Keep the item**, or it gets proposed again. |
| test a built item | it is in `LIVE-TEST`: on the VM run `git pull`, restart `bot.py`, and do the live check in its **Review notes** |
| say it works | tell Claude "IMP-… works". It becomes `DONE` and is tagged `release/<date>-IMP-…`. The next item can start. |
| say it failed | tell Claude what happened, with the log lines. It gets fixed on the same branch, or reverted if you want it dropped. |
| steer the direction | edit [[product]]. The loop proposes against it. |
| undo one | `git revert <its commit>`, or see [[runbook]] §8 |

### Status meanings
`PROPOSED` written by the loop, waiting for you · `APPROVED` you said yes, the
next `/improve` builds it · `IN-PROGRESS` being built · `LIVE-TEST` committed and
pushed, **waiting for your VM check** (nothing new is built meanwhile) · `DONE`
you confirmed it works live · `REJECTED` you said no · `BLOCKED` the loop could
not finish it, and the reason is on the item. (`READY-FOR-REVIEW` is from the
old branch-per-item flow; IMP-002 is the last item to use it.)

### Item template
```
### IMP-### — <title>
status: PROPOSED
kind: EPIC | SLICE (epic: IMP-###) | FIX
for: caller | supervisor | admin | developer   (see [[product]] §2)
source: product theme N | roadmap §x | bugs B-0xx | agent idea
size: S | M | L          (a SLICE or FIX is at most ~400 changed lines)
why (what they can do afterwards that they can't today):
acceptance criteria:
slices (EPIC only, in order; slice 1 useful on its own):
test plan (offline):
live check needed (on the VM):
risk / blast radius:
commit: —
why rejected: —
Review notes: —
```

---

## Items

### IMP-001 — "Are you still there?" instead of hanging up on a silent caller
status: DONE (live-verified on the VM 2026-09-28)
source: agent idea (product: call quality)
size: M
why (value for the call centre): today a caller who goes quiet (looking up an
  order number, a bad line, a muted phone) hears nothing, and after
  `engine.idle_timeout_s` (30 s) the call is cut off without a word ([[runbook]]
  §6: "Call ends after exactly 30 s"). A human agent would check in. A real call
  centre loses a caller it could have kept, and the caller has no idea why.
  Pipecat 1.6 already provides the hook: `user_idle_timeout` plus the
  `on_user_turn_idle` event on the user aggregator
  (`pipecat/processors/aggregators/llm_response_universal.py`), so no new
  dependency and no change outside `engine/` + `core/config.py`.
acceptance criteria:
  - new config `engine.turn_taking.reprompt_after_s` (default 10) and
    `engine.turn_taking.max_reprompts` (default 2). `0` disables it, restoring
    today's behaviour exactly.
  - after that much caller silence, the agent speaks a short check-in ("Are you
    still there?"). The count resets whenever the caller speaks.
  - after `max_reprompts` unanswered check-ins it says a polite goodbye and ends
    the call itself, so the call record's cause says "caller silent" instead of
    the generic idle timeout. It ends through the normal engine exit, so
    `run_call` still disconnects the caller and frees the slot (B-012).
  - config validation: rejects negatives; warns if reprompt_after_s ×
    (max_reprompts+1) ≥ idle_timeout_s (the hard timeout would fire first).
test plan (offline): the re-prompt counting/reset/give-up logic lives in a
  small pure class in `engine/` with its own unittest (no Pipecat pipeline
  needed). Config tests for defaults, `0` = off, validation. Layering test stays
  green.
live check needed (on the VM): call, say nothing. You should hear the check-in
  at ~10 s and ~20 s, then the goodbye, then the call ends. Then call again and
  answer the first check-in: the conversation carries on normally. Afterwards
  `group show channels` should be empty. Offline tests can't prove the event
  fires on real silence, or how it interacts with barge-in.
risk / blast radius: engine only; `0` turns it off. Main risk: a check-in that
  talks over a caller who is slow to start. Watch for that on the live call.
branch: improve/IMP-001-silence-reprompt (commit bc6fb22)
why rejected: —
Review notes:
  WHAT CHANGED, file by file
  - engine/silence.py (new, ~50 lines): a tiny counter, `SilencePolicy`. Every
    time Pipecat says "the caller has been quiet for 10 s", it answers either
    REPROMPT (ask "are you still there?") or GIVE_UP (say goodbye). When the
    caller speaks, the count goes back to zero. It has no Pipecat in it, so
    it can be tested on its own.
  - engine/pipecat_engine.py: (1) passes `user_idle_timeout` to Pipecat's user
    aggregator, which is what makes Pipecat fire `on_user_turn_idle`; (2) in
    run(), two small event handlers: caller started talking → reset the count;
    caller idle → queue the check-in line, or the goodbye + an EndFrame.
    EndFrame = "finish playing what's queued, then stop the pipeline".
  - core/config.py: four new optional settings under engine.turn_taking
    (reprompt_after_s, max_reprompts, reprompt_text, goodbye_text), each
    validated, plus one startup check (below).
  - config.yaml: the four settings written out with their defaults + comments.
  - tests/test_silence.py: 16 tests (counting, reset, config rules, and that
    the engine really hands the timeout to Pipecat).
  - docs: changelog, decisions 052, a runbook troubleshooting row.
  WHY THIS WAY
  - Fixed sentences, not the LLM: faster, free, and it can't ramble or
    decide to transfer. You can reword them in config.yaml.
  - Pipecat's timer restarts each time the agent finishes speaking and is
    cancelled when anyone talks, so no timer code of our own was needed.
  - One correction to the proposal: it said to warn if
    reprompt_after_s × (max_reprompts+1) ≥ idle_timeout_s. That formula was
    wrong, because the agent's own speech resets the 30 s idle timeout. The
    real problem case is reprompt_after_s ≥ idle_timeout_s: the check-in would
    never fire. It is now a startup ERROR, not a warning, which matches how the
    config loader treats every "setting that silently does nothing".
  - Ending the call: the engine just returns, and run_call's existing
    disconnect (the B-012 fix) hangs up and frees the agent. No new teardown
    code.
  HOW TO TEST ON THE VM (after `git pull` of this branch + restart bot.py)
  1. Call 6001 and say nothing after the greeting. Expected: ~10 s → "Are you
     still there?", ~10 s later again, ~10 s later the goodbye, then the call
     ends. Log shows `Caller silent -- check-in 1/2`, `2/2`, then
     `saying goodbye`. The call's cause (dashboard / calls.db) reads
     `caller silent -- no answer to 2 check-in(s)`.
  2. Call again, stay quiet until the first check-in, then answer. The
     conversation should carry on normally, and the next silence gets 2 fresh
     check-ins.
  3. After both: `asterisk -rx "core show channels"` and
     `asterisk -rx "group show channels"` are empty (no leaked slot).
  4. Listen for: the goodbye being cut off (it shouldn't be), or a check-in
     talking over you when you start speaking right at ~10 s.
  To switch it off without a rollback: `reprompt_after_s: 0` in config.yaml.
  ROLL BACK: don't merge, or `git branch -D improve/IMP-001-silence-reprompt`.
  The base before this item is tagged backup/2026-09-28-pre-IMP-001.

### IMP-002 — Break down the 3–6 s wait before the greeting
status: READY-FOR-REVIEW
source: roadmap §2 Stage E (open measurement items); Stage E load-test notes
  (greeting takes 3–6 s, and the engine only starts reading ~3.6 s after connect,
  cause unexplained)
size: S
why (value for the call centre): the first thing every caller hears is several
  seconds of silence, and callers hang up on silence. Nobody knows which step
  costs the time: UUID correlation, VAD build, pipeline build, the Deepgram and
  Gemini connections, or the greeting's TTS. You can't fix it without first
  measuring it. It is also the number Stage F capacity planning needs.
acceptance criteria:
  - each call logs one `setup:` line with milliseconds for each step: session
    accepted → engine start → VAD built → pipeline built → first agent audio frame
    actually sent (the AudioSocket write thread records the monotonic time of the
    first *real* frame).
  - `/metrics` exposes `voiceagent_time_to_greeting_seconds` (a sum + count pair,
    so an average can be computed; a histogram if it stays small).
  - **no database schema change.** `stores/sqlite_store.py` has no migrations
    (`CREATE TABLE IF NOT EXISTS` only), so a new column would silently fail to
    appear in the VM's existing `calls.db`. If it belongs in the record, that is
    a separate item: "add schema migrations".
test plan (offline): a unit test that the write thread stamps the first *real*
  frame and not the silence keep-alive. A test that the `setup:` line has every
  step. A `/metrics` test (`tests/test_api.py` pattern). A silent-engine load-test
  smoke run, because it touches `transports/`.
live check needed (on the VM): make 3 calls and read the `setup:` lines. That tells
  you which step is the 3–6 s. The fix itself is a later item, chosen from what
  this shows.
risk / blast radius: logging and metrics only; one timestamp in the write
  thread (one assignment, not per-frame work after the first).
branch: improve/IMP-002-greeting-timing (commit 47b0ddf)
why rejected: —
Review notes:
  WHAT CHANGED, file by file
  - engine/timing.py (new): two small functions. `setup_breakdown` takes a
    list of (moment, time) pairs and returns the gap between each pair in
    milliseconds, plus the total wait. `format_setup` turns that into one log
    line. No Pipecat, so it is tested on its own.
  - transports/audiosocket.py: two timestamps. `connected_at` (the socket
    arrived) and `first_real_out_at` (the first frame of AGENT speech was
    sent). The write thread sends silence from the very first moment to keep
    Asterisk happy, so "first frame sent" would be meaningless. It has to be
    the first REAL one. That costs one `if` per real frame.
  - core/transport.py: a new optional method on the call contract,
    `setup_marks()`. It returns {} by default, so other vendors don't have to
    implement it.
  - transports/asterisk.py: implements it: connected, correlated (the moment
    the call was matched to its ARI channel), and first_speech.
  - engine/pipecat_engine.py: stamps its own steps (engine start, VAD built,
    pipeline built, pipeline started = Deepgram and Gemini connected) and
    logs the `setup:` line when the call ends. It also returns the total in
    EngineResult.
  - core/engine.py, core/live.py, bot.py, api/server.py: the total flows into
    two counters and out on /metrics as `voiceagent_time_to_greeting_seconds`.
  - tests/test_setup_timing.py: 12 tests. docs: changelog, plus a runbook
    troubleshooting row explaining each step of the line.
  WHY THIS WAY
  - Each moment is stamped by whoever actually sees it, so no layer reaches
    into another. The layering test still passes.
  - time.monotonic(), not the wall clock, because the wall clock can jump.
  - No database column, because the SQLite store can't add columns to an
    existing database (it has no migrations). If you want this per call in
    calls.db, that is a separate item: "add schema migrations".
  - A caller who hangs up before the greeting is left out of the average. It
    wasn't a 0-second wait.
  HOW TO TEST ON THE VM (git pull this branch, restart bot.py)
  1. Make 3 calls to 6001, each long enough to hear the greeting.
  2. grep "setup:" in the console or logs/agent.jsonl. Each call shows a line like
     `setup: connected→correlated 40 ms | correlated→engine_start 5 ms |
      ... | pipeline_started→first_speech 1800 ms | first speech after 3400 ms`
     The biggest step is where the 3–6 s goes. That tells us what the NEXT
     item should fix.
  3. curl localhost:8091/metrics | grep greeting → `_count 3` and a `_sum`.
  4. One call to 6000 (direct, no ARI) should simply have no correlated step.
  Already verified here: 143 tests pass, and a silent-engine spike of 5
  callers was clean (0 dropped frames, 0 pacer slips). Only a real call can
  produce the actual numbers.
  MERGING WITH IMP-001: both branches edit PipecatEngine.run(), the top of
  changelog.md, and the same row area of runbook.md. Expect small merge
  conflicts. Keep both sides in each case.
  ROLL BACK: don't merge, or `git branch -D improve/IMP-002-greeting-timing`.
  The base before this item is tagged backup/2026-09-28-pre-IMP-002.

### IMP-003 — Back up the Asterisk configuration into the repo
status: APPROVED
source: roadmap §4 #7 and §5 #1 (`asterisk-config.md`)
size: M
why (value for the call centre): the dialplan (`[transfer]` departments, the
  `GROUP` capacity gate, the busy message), `ari.conf`, `http.conf` and the PJSIP
  endpoints exist **only on the VM**. Everything else is in git. If the VM is
  lost, transfer and capacity limiting go with it, and today the only way to get
  them back is from memory. That fits this loop's own backup goal: the code can
  be rolled back, but the telephony half of the system cannot.
acceptance criteria:
  - `tools/snapshot_asterisk.py`, run **by you on the VM**, copies the relevant
    `/etc/asterisk/*.conf` into `asterisk/` in the repo, with **secrets
    redacted** (`password=`, `secret=`, and PJSIP auth passwords become
    `<redacted>`). You review the diff and commit it yourself.
  - `--check` mode compares the live files with the committed snapshot and lists
    any drift, so you can tell when the VM has been hand-edited.
  - `docs-vault/asterisk-config.md` explains what each context does and how to
    restore it on a fresh Asterisk.
test plan (offline): unittest for the redaction on sample `.conf` text (every
  secret form is redacted; non-secret lines are byte-identical; comments are
  kept). A test for drift detection. Nothing here touches the running service.
live check needed (on the VM): you run the script once and read the diff to check
  no real password survived. Only the VM has the real files, so the loop can
  never run this part.
risk / blast radius: none to the running service (a read-only copy). The real
  risk is a secret getting into git, so the redaction tests are the core of the
  item, and you check the diff before committing.
branch: —
why rejected: —
Review notes: —
