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
not finish it, and the reason is on the item. · `PARKED` set aside on
purpose by you: it doesn't block the loop and isn't built until you set it back to
`APPROVED` (or `LIVE-TEST`). · `UNTESTED` built and pushed, but you chose to
move on before the live check: it doesn't block the loop, and the loop reminds
you of it at the end of each build until you report the result. (`READY-FOR-REVIEW` is from the
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
status: PARKED (2026-09-28, by request; code is in and pushed, live check still to do: tools/check_greeting_timing.py)
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
commit: on feature/multi-agent-pool (the commit titled `IMP-002: …`), brought over from the old improve/IMP-002-greeting-timing branch
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
  HOW TO TEST ON THE VM (`git pull`, then restart bot.py: Ctrl+C once, wait for
  `stopped cleanly`, start it again)
  QUICK WAY: after the calls below, run
     python tools/check_greeting_timing.py --since-minutes 10
  on the VM. It prints each call's steps, the slowest step, the /metrics
  count, and PASS/FAIL. (`--originate 3 --context <ctx>` places the calls for
  you through Asterisk.)
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
  COMBINED WITH IMP-001: brought onto the working branch after IMP-001. The
  overlaps (engine run(), changelog, runbook) were merged keeping both. 159
  tests pass (147 + 12), and a silent-engine spike of 5 was clean again.
  ROLL BACK: `git revert <the IMP-002 commit>` (then push), or go back to
  tag backup/2026-09-28-pre-IMP-002-linear.

### IMP-003 — Back up the Asterisk configuration into the repo
status: PARKED (2026-09-28, by request; not built)
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

### IMP-004 — Supervisor web app (EPIC)
status: APPROVED
kind: EPIC
for: supervisor (and later admin)
source: product theme 1
size: L (built as the slices below, each at most ~400 lines)
why (what they can do afterwards that they can't today): today a supervisor
  gets one read-only status page, reachable only over an SSH tunnel, that
  forgets every call the moment it ends. They cannot look up yesterday's
  calls, see why a call ended, read what a caller said, or log in from their
  own PC. Every call is already recorded in `records/calls.db`; there is just
  no way to see it.
acceptance criteria (for the epic as a whole): a supervisor opens a URL on the
  office network, logs in, sees live calls and agents (today's page), browses
  and filters past calls, opens one to read its transcript and outcome, and
  signs out, with no terminal and no SSH.
slices (in order; each works on its own and gets its own live check):
  1. **Call history, read-only** (IMP-005, DONE 2026-09-29). A "Recent calls" list on the
     existing page: time, caller, agent, duration, how it ended, transferred to.
     Needs the store's first READ path (below). Loopback only, like today.
  2. **Call detail + search** (IMP-007, DONE 2026-09-29). Click a call: its transcript from the `turns`
     table, cause, timings (IMP-002's `setup:` total, if stored), a filter by
     date / agent / outcome / caller number. Known gap to confirm first:
     agent-side turns may not be in `turns` yet (roadmap §2, deferred from
     Stage B); if not, this slice shows the caller side plus a link to
     `conversation.json`.
  2b. **Move to React** (IMP-011, DONE 2026-09-29).
  2c. **App shell + new design + Live page** (IMP-012, built; LIVE-TEST).
  2d. Superseded by the page plan in [[web-app-design]] (v2, accepted
      2026-09-29). Its build order replaces the remaining slices here:
      honest numbers (IMP-013..015) → Insights → couldn't get through →
      Calls + export → System health → Agents → login → listen/transfer →
      Settings.
  3. **Login** (IMP-010, proposed; after the new design, so it's built in it). One supervisor password (hash in `.env`, never in YAML), a
     session cookie, every page and API route behind it, plus a logout. This is
     what makes slice 4 safe; [[decisions]] 043 is exactly this gap.
  4. **Reachable from the office network.** Bind to a LAN address only when
     login is on (refuse to start otherwise), plus a runbook section on putting
     HTTPS in front (Caddy/nginx), because a password over plain HTTP can be
     sniffed.
  5. **Agent view.** Per-agent calls today, average duration, transfer rate.
     Bridges into theme 3 (admin), which edits agents from here.
  Decision needed BEFORE slice 3 (a separate item when we get there): stay
  with plain HTML + vanilla JS served by aiohttp (today's approach: no build
  step, no CDN, decisions 045), or adopt a frontend framework. Recommendation:
  stay plain until the page genuinely hurts to maintain.
test plan (offline): per slice.
live check needed (on the VM): per slice.
risk / blast radius: the API runs INSIDE the call process, so every new read
  must stay off the event loop (decisions 043). A slow query that blocks the
  loop drops live calls. Each slice carries that as a test.
commit: —
why rejected: —
Review notes: —

### IMP-005 — Recent-calls list in the supervisor page (slice 1 of IMP-004)
status: DONE (live-verified on the VM 2026-09-29)
kind: SLICE (epic: IMP-004)
for: supervisor
source: product theme 1
size: M
why (what they can do afterwards that they can't today): see the last calls
  that have ENDED (not just the ones in progress), with who called, which
  agent answered, how long it lasted, how it ended, and whether it went to a
  department, without opening SQLite by hand.
acceptance criteria:
  - `CallStore` gains its first read method, `recent_calls(limit, tenant_id)`.
    It is implemented for SQLite on its OWN read-only connection (WAL allows
    concurrent reads with the writer), run via `asyncio.to_thread`, so it
    never blocks the event loop and never contends with the single writer
    task. `NullCallStore` returns [].
  - new route `GET /history?limit=50` (capped at 200) returns those rows as
    JSON: call_id, started_at, duration_s, caller_id, persona, end_reason /
    cause, transferred_to.
  - the dashboard gets a "Recent calls" table under "Calls in progress",
    loaded on page open and refreshed when a live call ends (the WebSocket
    already pushes that change), not polled.
  - still loopback-only with no auth. It exposes caller numbers exactly like
    `/calls` already does, so no new exposure; login is slice 3.
test plan (offline): store test against a temp SQLite file (write 3 calls
  through the real writer, read them back newest-first, limit and tenant
  honoured); a test that the read runs in a thread (the loop stays responsive
  while a deliberately slow read runs); an API test for `/history` (shape, the
  limit cap, an empty store); `/metrics` still carries no caller numbers.
live check needed (on the VM): make 2 calls, open the dashboard (VS Code port
  forward 8091): both appear under "Recent calls" within a second of hanging
  up, newest first, with the right agent and "transferred to" for a transfer.
  Then hang up during a third call's greeting: it appears too.
risk / blast radius: touches `api/`, `core/records.py`, `stores/`, and the
  dashboard HTML. Silent-engine smoke run required. No schema change.
commit: on feature/multi-agent-pool (the commit titled `IMP-005: …`)
why rejected: —
Review notes:
  WHAT CHANGED, file by file
  - core/records.py: the call store gets its first READ method,
    `recent_calls(limit, tenant_id)`. The base class returns [] by default, so
    any store that can't read yet just shows no history instead of crashing.
    RecordWriter gets a matching pass-through, because the API only holds the
    writer.
  - stores/sqlite_store.py: the real query. It opens its OWN read-only
    connection each time and runs in a background thread, never on the event
    loop that serves calls. It returns only the columns the page needs
    (no file paths). A missing database file means "no calls yet", not an
    error.
  - api/server.py: new route `GET /history?limit=50` (max 200). A bad limit
    gives 400, a database error gives 503 (logged as `/history: …`), and
    records disabled gives an empty list. Also listed in `/api`.
  - api/dashboard.html: a "Recent calls" table under "Calls in progress". It
    loads when the page opens, and when a live call disappears it waits
    ~1.5 s (the record is written a moment after the call ends) and reloads.
    No polling.
  - tests/test_history.py: 14 tests, including one proving the event loop
    keeps running while a deliberately slow (0.3 s) read is in progress.
  - docs: decisions 054, changelog, and the runbook's dashboard section.
  WHY THIS WAY
  - The API lives inside the process that answers calls. Anything that blocks
    it drops calls (B-001, B-011). So the read goes to a thread, on a separate
    connection, so it never waits on, or delays, the writer.
  - No schema change, no new dependency, still one HTML file (decisions 045).
  - Same exposure as today: loopback only, no login (`/calls` already shows
    caller numbers). Login is slice 3 of IMP-004.
  ALREADY CHECKED HERE: 182 tests pass (168 + 14). A silent-engine spike of 5
  was clean, and afterwards `/history` returned those 5 calls, the page had
  the panel, and `?limit=x` gave 400.
  HOW TO TEST ON THE VM
  1. `git pull`, then `git log --oneline -1` should show the IMP-005 commit.
     Restart bot.py (Ctrl+C once, wait for `stopped cleanly`, start it again).
  2. Open the dashboard (VS Code PORTS → forward 8091, or
     `ssh -L 8091:localhost:8091 root@<vm>`), then http://localhost:8091/.
     "Recent calls" should already list older calls from calls.db, newest
     first.
  3. Keep the page open and call 6001. Talk briefly and hang up. Within ~2 s
     the call appears at the top: the right agent, length, caller, and
     "caller hung up".
  4. Call again and ask for billing. After the transfer, "Transferred to"
     shows `billing`.
  5. Optional: `curl -s localhost:8091/history?limit=2` prints JSON with the
     same two calls.
  If the table stays empty, check `service.records.enabled` and that
  `records/calls.db` exists on the VM.
  UNDO: `git revert <the IMP-005 commit>` and push, or return to tag
  backup/2026-09-28-pre-IMP-005.

### IMP-006 — `doctor`: one command that says what's wrong with a setup
status: PROPOSED
kind: SLICE (theme 2, "easy setup"; its epic gets written when a second setup slice exists, alongside IMP-003)
for: developer / installer
source: product theme 2
size: M
why (what they can do afterwards that they can't today): today a wrong setup
  shows up as a confusing failure on the first call (B-008 Gemini 404, B-011,
  B-013 port in use, missing ARI creds), and the fix is somewhere in an
  800-line runbook. `python tools/doctor.py` checks everything BEFORE a call
  and prints one plain line per problem, with the fix.
acceptance criteria: checks, each printed as PASS / WARN / FAIL with a
  one-line fix:
  - Python is 3.12, the venv is active, installed pipecat-ai matches the pin
    in requirements.txt (decisions 012)
  - config.yaml (or the given file) loads, with core/config.py's own error
    text shown
  - every `*_env` variable config.yaml names is SET (names only; values are
    never printed or logged)
  - ports 8090/8091 are free or held by bot.py (B-013 wording)
  - ARI answers at the configured URL with the configured user, and the
    Stasis app name matches
  - if `asterisk` is on PATH: extension 6001 exists, a `[transfer]` context
    has all four departments, and the busy file named in the runbook exists
    (the §4 traps)
  - `--providers` (opt-in, because it makes network calls): Deepgram and
    Gemini keys are accepted, using a free models/listing call, never a TTS or
    LLM call
  Exit code 0 when there are no FAILs.
test plan (offline): each check is a function returning (level, message),
  tested with fakes (a config fixture, a fake ARI HTTP server on localhost,
  monkeypatched shutil.which / subprocess). A test that no check can print an
  env var's value.
live check needed (on the VM): run it on the working VM: all PASS. Then break
  one thing at a time (stop Asterisk, rename an env var, start a second
  bot.py): each gives the right FAIL and fix line.
risk / blast radius: a new tool only; nothing in the call path changes.
commit: —
why rejected: —
Review notes: —

### IMP-007 — Call detail and search in the supervisor page (slice 2 of IMP-004)
status: DONE (live-verified on the VM 2026-09-29)
kind: SLICE (epic: IMP-004)
for: supervisor
source: product theme 1
size: M
why (what they can do afterwards that they can't today): click any recent call
  and read the whole conversation (what the caller said AND what the agent
  answered), with how it ended, the transfer, and how long the caller waited
  before the greeting; and narrow the list by date, agent, outcome or caller
  number. Today the only way to read a transcript is to SSH in and open a JSON
  file by hand.
acceptance criteria:
  - `GET /calls/<call_id>` returns the call row plus its transcript. **Where the
    transcript comes from:** the `turns` table holds only the CALLER side
    (engine/transcripts.py writes speaker="caller" only; agent turns were
    deferred in Stage B). The full two-sided conversation is in the call's
    `conversation.json`, whose path is already on the row. So: read that file,
    in a thread (no file I/O on the loop, decisions 054), and only if its
    resolved path is inside the recordings directory, never an arbitrary path
    from the database. Fall back to the caller-only `turns` rows if the file is
    gone.
  - `GET /history` gains optional filters: `since`/`until` (dates), `persona`,
    `outcome` (transferred / not), `caller` (substring). All are parameterised
    SQL (no string-built queries), and all still respect the 200-row cap.
  - The page: clicking a row opens a detail panel (conversation as a chat,
    caller left and agent right, plus cause, transfer and setup time). Above
    the table: date, agent and outcome pickers and a caller search box. Still
    one HTML file, no framework.
  - Unknown call id: 404. A malformed id: 400.
test plan (offline): store tests for each filter against a temp DB; detail
  tests for full transcript from conversation.json, fallback to turns, missing
  file, and a path outside recordings/ refused; a test that the file read
  happens off the loop; API tests (404/400, filters passed through); a page
  test that the detail panel and filter controls exist.
live check needed (on the VM): make 2 calls (one transfer). Click each: both
  sides of the conversation appear in order and the transfer shows. Filter by
  agent and then by "transferred": the list narrows correctly. Search by part
  of your extension number.
risk / blast radius: `api/`, `core/records.py`, `stores/`, dashboard. The only
  new risk is the file read, which is bounded to the recordings directory and
  run in a thread. Loopback-only, no new exposure.
commit: on feature/multi-agent-pool (the commit titled `IMP-007: …`)
why rejected: —
Review notes:
  WHAT CHANGED, file by file
  - core/records.py: `CallFilter` (the things you can filter by), plus two
    new read methods on the store: `search_calls(…, filters)` and
    `call_detail(call_id)`. Both return nothing by default, so older stores
    keep working. RecordWriter passes both through.
  - stores/sqlite_store.py: the filter becomes a SQL WHERE clause built only
    from bound parameters (browser input is never pasted into SQL). The
    caller search treats `%` and `_` as plain characters. `call_detail`
    returns the row plus the caller's turns. Same rule as before: its own
    read-only connection, in a thread.
  - api/server.py: `/history` reads the filters (a bad date or outcome gives
    400 with a reason). New `GET /history/<call_id>`: a bad id gives 400,
    unknown gives 404. The transcript comes from the call's
    conversation.json, which is the only place the agent's words are kept,
    read in a thread and only if the file is inside recordings/. Otherwise
    it falls back to the caller-only turns and says so. The file path is
    never sent to the browser.
  - api/dashboard.html: a filter bar above "Recent calls" (from/to date,
    agent, outcome, caller) with Filter and Clear. Clicking a row opens a
    "Call" panel with the facts and a chat view.
  - tests/test_call_detail.py: 19 tests (each filter, wildcard escaping,
    detail + fallback, a path outside recordings/ is never read, the file
    read stays off the event loop, 400/404, and the page elements).
  - docs: decisions 055, changelog, runbook. /improve's smoke run now uses
    its own ports (see below).
  WHY THIS WAY: see decisions 055. In short, the database only has the
  caller's side, the file has both, and the file path is confined so this
  can never become "read any file".
  OVER THE SIZE LIMIT: about 630 changed lines of code + tests (roughly 330
  code, 280 tests), against the loop's ~400-line rule. The filters and the
  detail view should have been two slices. Flagged here rather than trimmed
  after the fact, because cutting tests to fit the rule would be worse.
  ONE CHANGE FROM THE PROPOSAL: the route is `/history/<id>`, not
  `/calls/<id>`, because `/calls` means calls in progress.
  NOT INCLUDED (and why): the time-to-greeting figure in the detail view.
  IMP-002 logs it but never stored it per call (no schema change), so it
  would need a migration: a later item.
  ALREADY CHECKED HERE: 201 tests pass (182 + 19), and the JavaScript passes a
  syntax check. Smoke run on isolated ports 18090/18091: spike of 5 clean,
  the agent filter returned only that agent, a bad date gave 400, the detail
  route answered (source "none", correct for silent-engine calls, which
  write no transcript), an unknown id gave 404, and a bad id gave 400.
  INCIDENT DURING THE BUILD (harmless, fixed): the first smoke run's API
  couldn't bind laptop port 8091, because VS Code forwards it to the VM. My
  test requests (read-only GETs of /history) therefore reached the VM's live
  bot. The load test itself refused to run, because it saw a real engine, so
  no calls were placed. Fixed by moving smoke runs to 18090/18091 with an
  engine check (improve.md).
  HOW TO TEST ON THE VM
  1. `git pull` (expect the IMP-007 commit), restart bot.py.
  2. Open the dashboard (port 8091 forward), go to "Recent calls", and click
     yesterday's billing call (Alex, 2026-09-29 ~04:35 UTC). The chat should
     show the greeting, your request and "connecting you…", and "Transferred
     to" should say billing.
  3. Make a new short call, then click it: both sides appear in order.
  4. Filters: pick an agent and click Filter (only that agent's calls), then
     outcome "transferred" (only transfers), then type part of your extension
     in caller. Clear brings the full list back.
  5. A call from before Phase 2 (or a load-test call) should show the grey
     "no transcript" note, not an error.
  UNDO: `git revert <the IMP-007 commit>` and push, or return to tag
  backup/2026-09-29-pre-IMP-007.

### IMP-008 — When the AI model fails, the caller hears an apology and a human, not silence
status: UNTESTED (built + pushed 2026-09-29; live check still owed: see its Review notes)
kind: FIX
for: caller
source: live call 2026-09-28 17:47 (Gemini `503 high demand` after 16 s of silence; the caller hung up, and no transfer happened)
size: M
why (what they can do afterwards that they can't today): on 2026-09-28 a
  caller asked for billing, Gemini hung for 16 s and then returned 503, and the
  caller heard nothing at all until they gave up. The engine has no handling
  for an LLM error: Pipecat logs it as non-fatal and the call just goes quiet.
  With this, the caller hears "Sorry, I'm having trouble right now. Let me put
  you through to someone who can help", and is transferred to `human`, so the
  call still ends somewhere useful.
acceptance criteria:
  - `engine.llm.timeout_s` (default ~6 s): if the model has not started
    replying within that time after the caller finished speaking, treat it as a
    failure. (Pipecat has no such limit by itself; the call above waited 16 s.)
  - On an LLM error or timeout: speak a configurable apology line (config, not
    code), then transfer to the fallback department (`human` by default,
    configurable). On a direct 6000 call, where transfer is impossible, say
    goodbye and end the call instead. It happens at most once per call, and the
    record's cause says `llm failed: <short reason>`.
  - Optional `engine.llm.fallback_model`: retry once on a second model before
    apologising, e.g. `gemini-3.1-flash-lite` (measured ~650 ms). Off by
    default. Worth having because the 503 was "high demand" on one model.
  - No change when the model works.
test plan (offline): the decide-what-to-do logic (error or timeout → retry
  fallback model? → apologise + transfer, or goodbye if transfer is
  unavailable) as a small Pipecat-free class, like engine/silence.py, fully
  unit-tested. Config tests. Wiring tested by checking the engine registers the
  error handler and the timeout.
live check needed (on the VM): temporarily set `engine.llm.model` to a name
  that doesn't exist (instant error), call 6001 and ask something: you should
  hear the apology, then the transfer rings the human line, and the dashboard
  cause reads `llm failed`. Put the model back. The timeout path can only be
  seen during a real provider slowdown; say so rather than claim it.
risk / blast radius: engine only. The main risk is a false timeout on a slow
  but healthy reply: 6 s is well above the ~0.7 s normal latency measured for
  this model, and it is configurable.
commit: on feature/multi-agent-pool (the commit titled `IMP-008: …`)
why rejected: —
Review notes:
  WHAT CHANGED
  - engine/failover.py (new): the watchdog. It starts a clock when the model is
    asked for a reply, stops it on the model's first real output (text or a
    tool call), and trips on (a) no output within timeout_s or (b) an error
    raised BY the model. It trips once per call. Two tiny pipeline components
    sit either side of the LLM to feed it, and the "after" one also drops
    any late reply once it has tripped.
  - engine/pipecat_engine.py: puts those components around the LLM, and on a
    trip speaks the apology then transfers (after the same 3 s announce pause
    as normal transfers), or says goodbye on a non-transferable call. Checks
    at start-up that failover.department is a real department. The cause
    "llm failed -- ..." is kept even though the transfer then ends the call.
  - core/config.py + config.yaml: `engine.failover` (enabled, timeout_s 2-30,
    department, apology_text, goodbye_text), validated like everything else.
  - tests/test_failover.py (16): timing (fires on silence, not when answered,
    once per call, restarts per turn, stops cleanly), taps (a request starts
    it; only the MODEL's errors count; text or a tool call counts as an
    answer, but Pipecat's start marker doesn't; late text is dropped), config
    validation, and the engine refusing an unknown department.
  - docs: decisions 058, changelog, runbook troubleshooting row.
  WHY THIS WAY: see decisions 058. The key finding: Pipecat's "response
  started" marker is sent BEFORE Gemini is even called, so it can't tell a
  hang from a reply; the watchdog waits for real output instead.
  LEFT OUT: the optional backup model (you have one LLM). It can be added
  later as a retry before the apology.
  ALREADY CHECKED HERE: 225 tests pass (209 + 16). Mutation check: letting any
  component's error count makes the "only the model's errors" test fail. No
  smoke run needed: engine only (the silent engine doesn't use it).
  NOT CHECKED HERE: a real call. The live check below forces a real model
  error, which is the only honest proof.
  HOW TO TEST ON THE VM
  1. `git pull`, restart bot.py.
  2. Force a model error: in config.yaml set `engine.llm.model` to a name that
     doesn't exist (e.g. `gemini-does-not-exist`) and restart. (The model is
     only used when the caller speaks, so the greeting still plays.)
  3. Call 6001, hear the greeting, ask anything. Within a few seconds you
     should hear "Sorry, I'm having trouble right now. Let me put you through
     to someone who can help." and then the human line (102) rings.
     Log: `AI model failed (model error: ...) -- apologising and transferring
     to human`. Dashboard → Calls → that call: "How it ended" reads
     `llm failed -- model error: ...`, and "Transferred to" says human.
  4. Call 6000 (direct, can't transfer) and ask something: you hear the
     goodbye line and the call ends.
  5. **Put the real model name back** and restart. Make a normal call: no
     change in behaviour.
  (The timeout path, a model that hangs, can't be forced on demand; it uses
  the same hand-over code as step 3.)
  UNDO: `engine.failover.enabled: false` (no code change), or
  `git revert <the IMP-008 commit>`, or tag backup/2026-09-29-pre-IMP-008.

### IMP-009 — Decide: keep the web app as plain HTML, or adopt a frontend framework
status: DONE (decided 2026-09-29: option C, React + Vite + TypeScript; see decisions 056)
kind: DECISION (for epic IMP-004)
for: supervisor (what they get) and developer (what it costs to change)
source: product theme 1; IMP-004 said to decide before slice 3
size: S (a decision and a decisions.md entry; no code)
why (what they can do afterwards that they can't today): nothing new for a
  user. It settles HOW the next three slices and the admin screens (theme 3)
  get built, before login makes the page multi-screen.
the options, honestly:
  A. **Stay plain** (recommended now). One HTML file served from memory, no
     build step, no npm, no CDN (the VM may have no internet; decisions 045).
     Today it is ~400 lines and still readable. Login adds a second small page.
     Cost: it gets harder to maintain around 1,000+ lines or several screens.
  B. **A small no-build library** (e.g. Preact + htm, or Alpine.js) vendored
     as ONE file into `api/static/`, not loaded from a CDN. Components without
     a build step. Cost: one more thing to learn, and a vendored file to update
     by hand.
  C. **A full framework** (React/Vue/Svelte + Vite). The nicest to build big
     UIs in. Cost: Node and npm on the dev machine, a build step before every
     deploy, a second language toolchain for a small team, and the built files
     either committed or built on the VM.
recommendation: **A now, with a trigger to revisit:** move to B when the page
  passes ~1,000 lines or theme 3 (admin screens with forms) starts. Written
  into decisions.md so the question is not re-argued each slice.
acceptance criteria: you pick A, B or C; the choice and its trigger go into
  decisions.md; IMP-010 follows it.
test plan (offline): none (a decision).
live check needed (on the VM): none.
risk / blast radius: none now. Choosing C later is a real migration, which is
  why the trigger matters.
commit: —
why rejected: —
Review notes: —

### IMP-010 — Login for the supervisor page (slice 3 of IMP-004)
status: PROPOSED (re-scoped 2026-09-29: built in React, in the new design, after IMP-012/013)
kind: SLICE (epic: IMP-004)
for: supervisor
source: product theme 1; decisions 043 (the page has no auth, which is why
  it is loopback-only)
size: M
why (what they can do afterwards that they can't today): the page and every
  API route that shows caller numbers or transcripts require a password.
  That is the precondition for slice 4: opening it to the office network
  instead of an SSH tunnel. On its own it also stops anyone who reaches the
  VM's loopback (another user, another service) from reading call data.
acceptance criteria:
  - one supervisor password, stored only as a salted hash (stdlib
    `hashlib.scrypt`) in `.env` as `SUPERVISOR_PASSWORD_HASH`, referenced from
    config as `service.api.password_hash_env` (the same *_env pattern as the
    API keys). `python tools/set_password.py` prompts for the password and
    prints the line to paste into `.env`, so it never touches a file itself.
  - a session cookie signed with HMAC (secret: `SUPERVISOR_SESSION_SECRET` in
    .env, or generated at start-up, which logs everyone out on restart;
    stated). HttpOnly, SameSite=Strict, 12 h lifetime, and Secure when
    behind HTTPS.
  - `/login` (a React page in the new app, see IMP-011 / decisions 056) and `/logout`. Protected: `/`,
    `/api`, `/pool`, `/calls`, `/history*`, and `WS /live`. **Open**: `/health`
    (load balancers) and `/metrics` (numbers only, already tested to carry no
    caller data), both documented.
  - wrong password: a slow, generic "wrong password" with a small per-IP
    lockout after 5 tries in a minute. Nothing logs the password.
  - **login is off unless configured**, so today's VM keeps working unchanged
    until you set the hash; start-up logs clearly whether it is on. Binding to
    a non-loopback host WITHOUT login stays a loud warning (slice 4 turns that
    into a refusal).
  - no new dependency (no aiohttp-session): hashing and signing are stdlib.
test plan (offline): hash/verify round-trip and wrong password; cookie
  tamper, expiry and wrong secret rejected; every protected route gives 401
  (API) or a redirect to /login (page) without a cookie, and 200 with one;
  /health and /metrics stay open; WS /live refuses without a cookie; lockout
  after 5 failures; login off = today's behaviour exactly; config validation;
  the set_password tool prints a hash that verifies.
live check needed (on the VM): run `tools/set_password.py`, paste the lines
  into .env, restart. Open the dashboard: you land on /login. A wrong password
  is refused; the right one shows the dashboard with live updates. Logout
  returns you to /login. Restart bot.py: still logged in if the session
  secret is in .env, logged out if not. `curl localhost:8091/history` without
  a cookie gives 401; `curl localhost:8091/health` still works.
risk / blast radius: `api/` and `core/config.py` only; nothing in the call
  path. The main risk is locking yourself out: removing the hash from .env
  turns login off again, and that is documented.
commit: —
why rejected: —
Review notes: —

### IMP-011 — Rebuild the supervisor page in React, same features (web app slice 2b)
status: DONE (live-verified on the VM 2026-09-29)
kind: SLICE (epic: IMP-004)
for: supervisor (nothing changes for them yet) and developer (everything after this builds faster)
source: decisions 056 (the human chose React on IMP-009)
size: L in files, M in hand-written lines (generated files don't count, per decisions 056)
why (what they can do afterwards that they can't today): for supervisors,
  the SAME page: capacity, agents, totals, audio health, live calls, recent
  calls with filters, call detail. Deliberately no new features, so if anything
  differs it's the port's fault. For development, every later screen (login,
  agent view, admin) gets built in React components instead of one growing
  HTML file.
acceptance criteria:
  - `web/`: Vite + React + TypeScript app (`npm run dev|build|test`).
    Components: Header, Capacity, Agents, Totals, AudioHealth, LiveCalls,
    RecentCalls (with Filters), CallDetail; a `useLive()` hook for `WS /live`
    (reconnects, same as today); an `api.ts` for `/history` and
    `/history/<id>`.
  - `npm run build` writes to `api/static/` plus `build-info.json` (a hash of
    the source and lockfile). Built files are committed.
  - aiohttp serves `api/static/` (index.html at `/`, and hashed assets with
    long cache headers). **`api/dashboard.html` is removed** once parity is
    confirmed, in the same commit, so there is one page and not two.
  - Behaviour kept exactly: push-only live updates, durations ticked in the
    browser, the recent calls list refreshed ~1.5 s after a call ends,
    everything escaped (React does this by default), the same colours and
    light/dark mode.
  - Dev proxy targets the local bot on 18091 (never 8091).
  - Every dependency is listed in the review notes with why.
test plan: **frontend** (Vitest + React Testing Library): each component
  renders sample state; the live hook applies a push and reconnects on close;
  recent calls re-fetch after a call leaves the live list; filters build the
  right query; call detail shows chat / "caller-only" / "no transcript".
  **Python** (unittest): `/` serves the built index.html; an asset is served
  with a cache header; the staleness guard fails when `web/src` changes
  without a rebuild; the existing API tests are unchanged and still pass.
live check needed (on the VM): `git pull`, restart, open the dashboard. It
  should look and behave like today: a live call appears and ticks; after
  hang-up it moves into recent calls; filters and click-to-detail work;
  light and dark mode both render. **The VM needs no Node**: confirm that by
  NOT installing it.
risk / blast radius: the page only; the call path and the JSON API are
  untouched. The main risk is a missed behaviour in the port (e.g. the 1.5 s
  refresh), which the component tests list explicitly. Rollback is
  `git revert`, which brings dashboard.html back.
commit: on feature/multi-agent-pool (the commit titled `IMP-011: …`)
why rejected: —
Review notes:
  WHAT CHANGED
  - web/ (new): the React app. The pieces, each a small file:
      src/useLive.ts        connects to WS /live, reconnects after 2 s, and
                            reports "connecting / live / disconnected". It
                            also notices when a live call disappears.
      src/api.ts            the two fetches (/history, /history/<id>) and
                            the filter query.
      src/components/       Summary (the 4 top panels + header), LiveCalls,
                            RecentCalls (+ filter bar), CallDetail (chat).
      src/App.tsx           wires them together; reloads recent calls
                            1.5 s after a call ends (same as before).
      src/styles.css        copied UNCHANGED from the old page.
      scripts/build-info.mjs  writes the source fingerprint after a build.
      .npmrc                a workaround for an npm 10.9 bug (see runbook).
  - api/static/ (new, GENERATED, committed): the built page. Never edit it by
    hand; run `npm run build`.
  - api/server.py: serves api/static/index.html at `/` and the files in
    api/static/assets at `/assets/<name>`. The old dashboard.html is deleted.
  - tests/test_web_build.py (new, 6 tests): build is current, the page only
    loads its own files, node_modules is ignored, serving, cache headers, and
    no path escape. Three older tests that read dashboard.html now read the
    React source AND the shipped bundle (same intent, same count).
  - .gitignore: web/node_modules/.
  - docs: runbook "Changing the web app", changelog. (decisions 056 already
    covers the design.)
  PACKAGES (all in web/package.json), and why:
    react, react-dom                          the UI library you chose
    vite, @vitejs/plugin-react                the build tool + its React plugin
    typescript, @types/react, @types/react-dom  type checking (tsc --noEmit)
    vitest, jsdom, @testing-library/react, @testing-library/dom
                                              frontend tests in a fake browser
  Versions are pinned by package-lock.json. npm first installed Vitest 3,
  which carries its own older Vite and clashed with Vite 8; upgraded to
  Vitest 5, which supports Vite 8.
  SIZE (measured): 1,055 hand-written lines added (335 of them tests), 414
  removed (mostly the old 396-line dashboard.html). The generated
  files (package-lock.json, api/static/) are larger and, per decisions 056,
  don't count. This is over the ~400 rule; porting a whole page is one
  indivisible step, which is why it was sized L.
  ALREADY CHECKED HERE: 207 Python tests (201 + 6) and 14 frontend tests pass.
  Type check is clean. Mutation checks: breaking the 1.5 s refresh fails a
  frontend test; editing a source file without rebuilding fails the staleness
  test; Windows line endings do NOT (so the VM agrees). Smoke on isolated
  ports 18090/18091: spike of 5 clean, `/` served with no-cache, the JS and CSS
  served with a 1-year cache, and a path-escape attempt gave 404.
  NOT CHECKED (needs your eyes): how it LOOKS in a real browser. No browser
  here; the tests use a simulated one.
  HOW TO TEST ON THE VM
  1. `git pull` (expect the IMP-011 commit), restart bot.py. **Do not install
     Node on the VM**: part of the test is that it isn't needed.
  2. Open the dashboard (port 8091 forward) and hard-refresh once
     (Ctrl+Shift+R), so the browser drops the old page.
  3. It should look like before: dark or light to match your system,
     capacity, agents, totals, audio health. The corner shows "live".
  4. Call 6001: the call appears under "Calls in progress" and its duration
     ticks. Hang up: ~2 s later it's at the top of "Recent calls".
  5. Filters (agent, outcome, caller, dates) and Clear work; clicking a call
     opens the chat view.
  6. Stop bot.py for a few seconds: the corner says "disconnected", then
     "live" again after you restart it, without reloading the page.
  If anything looks different from before, that's a bug in the port: tell me
  what.
  UNDO: `git revert <the IMP-011 commit>` brings dashboard.html back; or tag
  backup/2026-09-29-pre-IMP-011.

### IMP-012 — App shell, new design and the Live page (web app slice 2c)
status: PARKED (2026-09-29, by request: backend first, frontend later. Code is in and served; live check still to do)
kind: SLICE (epic: IMP-004)
for: supervisor
source: the human's direction ("the old dashboard was a rough starting point"); decisions 057
size: M (hand-written)
why (what they can do afterwards that they can't today): the app stops being
  one long page. It gets a sidebar (Live, Calls, Agents) and a Live page
  designed for glancing at from across a room: who is on a call right now,
  with whom, for how long, and whether anything is wrong. Everything after
  (the Calls and Agents pages, login, admin) plugs into the same shell and
  looks consistent.
acceptance criteria:
  - **Packages (named here, per the loop's rules):** `tailwindcss` +
    `@tailwindcss/vite` (styling, build time only) and `lucide-react` (icons).
    If Tailwind's Vite plugin doesn't support Vite 8, stop and report; don't
    downgrade Vite.
  - **Shell:** left sidebar with the product name, nav (Live / Calls /
    Agents, icon + label, current page highlighted), and at the bottom the
    connection status (live / reconnecting / offline) and uptime. Below
    ~768px it collapses into a top bar with a menu button.
  - **Routing:** `/#/live` (default), `/#/calls`, `/#/agents`, via a small
    hash router in `web/src/router.ts`. Back/forward work. An unknown hash
    goes to Live.
  - **Design tokens** in `web/src/theme.css`: brand accent, status colours
    (free = green, on call = amber, warning/error = red), neutral surfaces for
    light and dark (following the system), radius and spacing scale. The old
    styles.css is deleted.
  - **Live page, redesigned:**
      - a KPI row: agents on call / capacity (with a small bar), calls answered
        today, transferred, failed or turned away (red only when non-zero),
        audio health as a single OK / "N issues" badge
      - **agent cards**, one per agent: name, status pill, and when on a call
        the caller number and a ticking duration (joined from the live calls by
        agent name). Free agents are visually quiet; busy ones stand out
      - live calls as a compact list, longest first
      - loading skeleton before the first push; "offline, reconnecting…" banner
        when the socket is down (instead of stale numbers looking current)
  - **Calls and Agents pages** exist in the nav and, for this slice, show
    today's Recent calls / Call detail components inside the new shell
    (restyled with the tokens) and a simple agent list, so nothing is lost.
    Their full redesign is IMP-013.
  - Accessibility: every nav item and card is keyboard-focusable with a visible
    focus ring; colour is never the ONLY signal (pills also carry text);
    contrast at WCAG AA in both themes.
test plan: Vitest: the router (default, unknown hash, back/forward), the
  sidebar marking the current page, agent cards joining live calls to agents
  and ticking, the KPI warning state only when non-zero, the loading skeleton
  and offline banner, and the collapsed top bar at a narrow width. Python:
  staleness guard and serving tests stay green. Silent-engine smoke on
  18090/18091.
live check needed (on the VM): hard-refresh the dashboard. The sidebar is
  there, and Live is the default. Call 6001: that agent's card turns "on call",
  shows your number and ticks; the KPI updates; hang up and it returns to
  free. Stop bot.py: the offline banner appears; restart and it clears.
  Resize the window narrow: the sidebar becomes a top bar. Check light and
  dark mode. **Tell me what you don't like about the look**: this is the
  design everything else builds on, so now is the cheap time to change it.
risk / blast radius: the web app only; no Python behaviour changes. Rollback
  is `git revert` (the IMP-011 page returns).
commit: on feature/multi-agent-pool (the commit titled `IMP-012: …`)
why rejected: —
Review notes:
  WHAT CHANGED
  - web/src/theme.css (new): the design tokens, meaning every colour by its
    purpose (surface, muted text, free/on-call/problem, brand) for light and
    dark. Replaces styles.css. This is the file to edit for a rebrand.
  - web/src/router.ts (new, ~25 lines): reads `#/live`, `#/calls`, `#/agents`
    from the address bar; anything else goes to Live.
  - web/src/components/Shell.tsx (new): the sidebar / small-screen top bar,
    nav, connection status, plus shared pieces (PageHeader, Card, StatusPill).
  - web/src/pages/LivePage.tsx (new): headline numbers, audio badge, agent
    cards, live calls, loading skeleton, offline banner.
  - web/src/pages/CallsPage.tsx + AgentsPage.tsx (new).
  - RecentCalls.tsx / CallDetail.tsx: restyled; same behaviour; rows now
    open with Enter or Space too.
  - Summary.tsx, LiveCalls.tsx, styles.css: removed (replaced by the above).
  - tests: web/src/App.test.tsx rewritten for the new screens. Every IMP-011
    behaviour is still tested, plus the router, nav, agent cards, skeleton,
    offline banner and small-screen menu (14 → 21 tests).
    tests/test_theme_contrast.py (new, 2 tests): WCAG AA for every colour
    pair in both themes.
  - api/static/: rebuilt.
  PACKAGES added (named in this item): tailwindcss + @tailwindcss/vite
    (styling, build time only; supports Vite 8) and lucide-react (icons; only
    the 8 icons used are bundled). The page grew from 228 KB to 243 KB of JS
    (76 KB compressed).
  TWO DIFFERENCES FROM THE PROPOSAL, both honest limits:
  - The KPI says "Answered since the last restart", not "today". The live
    counters count from bot start; "today" needs a database query, and that
    belongs with the Agents/stats slice (IMP-013 / slice 5).
  - "Collapses on small screens" is built (a CSS breakpoint at 768px) and the
    menu toggle is tested, but the actual collapse is CSS, which the test
    browser doesn't lay out. Your resize check is the real test.
  CAUGHT BY CHECKING, NOT ASSUMED: the first draft's colours failed AA twice
  (the current-page highlight in light mode was 4.49:1; white text on the
  dark-mode Filter button was 2.4:1). Fixed, and now enforced by a test.
  ALREADY CHECKED HERE: 209 Python tests (207 + 2), 21 frontend tests, clean
  type check, build current. Mutation checks: breaking how calls are matched
  to agents fails 2 tests; white-on-light-blue fails the contrast test with the
  exact pair named. Smoke on 18090/18091: spike of 5 clean, new page and
  assets served.
  NOT CHECKED: how it looks in a real browser (no browser here).
  HOW TO TEST ON THE VM
  1. `git pull` (expect the IMP-012 commit), restart bot.py, and open the
     dashboard with a hard refresh (Ctrl+Shift+R).
  2. The sidebar is on the left, Live is highlighted, and the corner says
     "Live" with a pulsing green dot.
  3. Call 6001: that agent's card turns amber "On call" with your number and
     a ticking timer; "Agents on call" goes up. Hang up: back to "Free".
  4. Calls: the list and, after you click a call, the chat beside it. Agents:
     the roster.
  5. Stop bot.py for a few seconds: a red "Offline — reconnecting" banner and
     status appear; restart and they clear on their own.
  6. Make the window narrow (or open it on your phone through the tunnel):
     the sidebar becomes a top bar with a menu button.
  7. Switch your OS between light and dark mode: both should look deliberate.
  8. **Tell me what you don't like.** Spacing, colours, what's on the Live
     page, what's missing. This is the design everything else builds on, so
     now is the cheap time to change it. (Colours: one file, see runbook.)
  UNDO: `git revert <the IMP-012 commit>` (back to the IMP-011 page), or tag
  backup/2026-09-29-pre-IMP-012.

### IMP-013 — Let the call database grow: add columns safely, and record two new facts per call
status: DONE (live-verified on the VM 2026-09-29: 1 on a mid-sentence hang-up, 0 after the agent's goodbye)
kind: SLICE (epic: IMP-004; step 1 of [[web-app-design]] "honest numbers", part 1 of 3)
for: supervisor (indirectly: the facts the next slices need), developer
source: web-app-design v2; IMP-002 noted the store has no migrations
size: S–M
why (what they can do afterwards that they can't today): nothing visible yet.
  Today the database can never gain a column: `CREATE TABLE IF NOT EXISTS`
  silently skips the VM's existing `calls.db`, so any new per-call fact would
  quietly never be saved. This makes adding columns safe, then records the two
  facts the honest numbers need.
acceptance criteria:
  - **Additive migrations in `stores/sqlite_store.py`:** on start, compare the
    `calls` columns (`PRAGMA table_info`) with the schema and `ALTER TABLE ADD
    COLUMN` any that are missing, with a default. Only ever ADD: never drop,
    rename or rewrite. It is idempotent (starting twice changes nothing) and
    logs each column it adds. Existing rows keep their data.
  - **New per-call facts**, saved on every call from now on:
      - `agent_speaking_at_end` (0/1): was the agent mid-sentence when the call
        ended? The AudioSocket write thread already knows when it last sent
        real speech (it stamps the first; now also the last). "Speaking" means
        real audio sent in the last ~0.5 s before the end.
      - `time_to_greeting_s`: IMP-002's measurement, which is only in the log
        today (needed by System health and the Calls timing strip later).
  - Old rows read these as NULL ("not recorded"), never as 0/false, so older
    calls are never mislabelled.
test plan (offline): migrate a database created with the OLD schema (rows
  survive, columns appear, a second start is a no-op); a fresh database gets
  everything; the write thread stamps the last real frame; a call that ends
  during speech records 1, silence records 0; the record carries
  time_to_greeting_s. Silent-engine smoke on 18090/18091.
live check needed (on the VM): `git pull`, restart: the log shows
  "added column agent_speaking_at_end" and "added column time_to_greeting_s"
  ONCE (restart again: nothing). Old calls still show on the dashboard. Make 2
  calls: hang up once while the agent is talking and once in silence, then run
  `sqlite3 records/calls.db "select agent_speaking_at_end,
  time_to_greeting_s from calls order by started_at desc limit 2"`: 1 and 0,
  with a time for each.
risk / blast radius: touches the live database on start-up. Mitigated by
  additive-only changes, a test on an old-schema copy, and a backup step in the
  live check (copy calls.db first).
commit: on feature/multi-agent-pool (the commit titled `IMP-013: …`)
why rejected: —
Review notes:
  WHAT CHANGED
  - stores/sqlite_store.py: `ADDED_CALL_COLUMNS` (the list of columns added
    after databases existed) and `_migrate()`: on start it looks at which
    columns the table actually has and ADDs the missing ones, logging each.
    Nothing is ever dropped or rewritten. Starting twice does nothing.
    The two new columns are also in the table layout for brand-new databases.
  - transports/audiosocket.py: the audio connection now also stamps the LAST
    moment real agent speech went out, and the moment the call ended.
  - transports/asterisk.py: turns those into `agent_speaking_at_end`: True if
    real speech went out in the last 0.3 s before the end, False if the agent
    was quiet (or never spoke), None while the call is still up.
  - core/records.py + bot.py: the call record carries `agent_speaking_at_end`
    and `time_to_greeting_s` (IMP-002's number, until now only in the log).
  - tests/test_migrations.py (12): an old-layout database (built exactly like
    the VM's) gains the columns and keeps its rows; old rows read NULL, not 0;
    a second start adds nothing; a fresh database needs nothing; a migrated
    database accepts new records; the speaking-at-end rule in all four cases;
    the end is stamped once; the record carries both facts.
  - docs: decisions 059, changelog, runbook "Backing up the call database".
  CHECKED ON REAL DATA: a copy of the laptop's populated load-test database
  (60 old-layout rows) upgraded with every row kept, the oldest row unchanged,
  and `integrity_check` ok. Mutation check: switching the column-adding off
  fails 3 tests. Smoke on 18090/18091: spike of 5 clean; start-up logged both
  "added column" lines; the new calls were saved with the new fact filled in.
  237 tests pass (225 + 12).
  FOUND ON THE WAY: copying calls.db to back it up can miss recent data (WAL
  mode keeps it in calls.db-wal). The live check uses SQLite's backup instead.
  HOW TO TEST ON THE VM
  1. **Back up first**, from the repo folder:
       python -c "import sqlite3; s=sqlite3.connect('records/calls.db'); d=sqlite3.connect('records/calls-backup.db'); s.backup(d); d.close(); print('backed up')"
  2. `git pull`, restart bot.py. The start-up log shows, once:
       Records: added column calls.agent_speaking_at_end (INTEGER) ...
       Records: added column calls.time_to_greeting_s (REAL) ...
     Restart again: those lines do NOT appear.
  3. Open the dashboard → Calls: all your old calls are still there.
  4. Make 2 calls: in one, hang up WHILE the agent is talking; in the other,
     wait for the agent to finish, pause, then hang up. Then:
       python -c "import sqlite3; c=sqlite3.connect('records/calls.db'); print(c.execute('select caller_id, agent_speaking_at_end, time_to_greeting_s from calls order by started_at desc limit 2').fetchall())"
     Expect (newest first) something like `[('100', 0, 1.8), ('100', 1, 1.7)]`:
     0 for the pause-then-hang-up, 1 for hanging up mid-sentence, and a
     greeting time on both.
  UNDO: the new columns are harmless if left (nothing reads them yet), so
  `git revert <the IMP-013 commit>` is enough; or restore
  records/calls-backup.db; or tag backup/2026-09-29-pre-IMP-013.

### IMP-014 — "Resolved by the AI", "Unclear", and a business time zone
status: PARKED (2026-09-29: backend focus; the outcome rule returns with the Insights work)
kind: SLICE (epic: IMP-004; "honest numbers", part 2 of 3)
for: supervisor, manager
source: web-app-design v2 (definitions section)
size: M
why (what they can do afterwards that they can't today): every finished call
  gets one plain outcome that doesn't flatter the AI: Resolved by AI, Sent to
  <department>, Unclear (with the reason), Caller went quiet, Failed. The
  Calls page shows it as a chip and can filter by it. "Today" means today in
  your business's time zone, not UTC.
acceptance criteria:
  - **The rule, in ONE place** (a small pure function, e.g. `core/outcomes.py`)
    so every page agrees: transferred → Sent to a human; engine failure →
    Failed; the IMP-001 check-in ended it → Caller went quiet; otherwise
    Resolved **only if** it lasted > 10 s AND `agent_speaking_at_end` is not 1
    AND the same caller didn't call again within 24 h; else Unclear with the
    reason ("hung up mid-answer", "very short", "called back"). Calls from
    before IMP-013 (NULL) are judged on what they have and marked "(older call,
    less data)". The thresholds are config, not code.
  - `/history` rows carry `outcome` and `outcome_reason`; the outcome filter
    gains resolved / unclear / failed / went quiet. The repeat-call check is
    one indexed query (caller_id + time), off the event loop as usual.
  - **Business time zone:** `service.timezone` (default `UTC`, validated at
    start-up), used for date filters. **Adds the `tzdata` package** to
    requirements.txt: Windows has no time-zone database, so without it the
    laptop can't run the tests (the Linux VM would work either way).
  - Calls page: an outcome chip per row (the colours from theme.css) and the
    new outcome options in the filter.
test plan (offline): the outcome function over every branch and edge (10 s
  exactly, NULL facts, callback at 23 h vs 25 h, transferred AND short = sent to
  a human); config validation of the time zone and thresholds; `/history`
  outcome + filter against a temp database; date filters across a time-zone
  boundary (23:30 UTC is "tomorrow" in some zones); frontend: chips render and
  filter.
live check needed (on the VM): set `service.timezone: Europe/London` (or
  yours). Make 3 calls: one normal conversation, one where you hang up while
  the agent is talking, one you call back straight after. On Calls: the first
  shows Resolved by AI; the second shows Unclear · hung up mid-answer; the
  first number's earlier call flips to Unclear · called back. Filter by Unclear.
risk / blast radius: API + one config key + one small dependency; nothing in
  the call path.
commit: —
why rejected: —
Review notes: —

### IMP-015 — Mask caller numbers by default
status: PARKED (2026-09-29: folded into IMP-019, privacy)
kind: SLICE (epic: IMP-004; "honest numbers", part 3 of 3)
for: supervisor (and anyone who can see their screen)
source: web-app-design v2 ("private by default"); roadmap §5 privacy gap
size: S
why (what they can do afterwards that they can't today): the dashboard can
  sit on a shared screen without showing everyone's phone numbers: `+44 •••
  0123`. Before login exists this is the safe default; after login (IMP-010),
  logged-in supervisors see full numbers again.
acceptance criteria:
  - `service.api.mask_caller_numbers` (default **true**). Masking happens on
    the SERVER in every response and live push that carries a number
    (`/calls`, `/history`, `/history/<id>`, `WS /live`), so full numbers never
    reach the browser. Keep the country prefix and the last 4 digits; short
    internal extensions such as `100` are shown as-is.
  - Caller search still works on the full number (the server searches, then
    masks the result).
  - With it set to false, today's behaviour exactly.
test plan (offline): the mask function (international, national, short
  extensions, "unknown"); every endpoint and the live push masked when on and
  full when off; search by digits still finds a masked call; `/metrics` still
  has no numbers.
live check needed (on the VM): restart, open the dashboard: numbers show as
  `+44 ••• 0123` (your SIP extension `100` stays `100`). Search Calls by part
  of a number: still found. Set it to false, restart: full numbers again.
risk / blast radius: API only; a display change.
commit: —
why rejected: —
Review notes: —

### IMP-016 — Measure, then cut, how long the agent takes to reply
status: LIVE-TEST (the measuring half; the fix follows from your numbers)
kind: SLICE (backend focus #3; after IMP-013)
for: caller
source: Stage E laptop measurements (2.3–2.6 s reply, one 11.6 s); voice-agent research (good: under 1.5 s typical)
size: M (the measuring part S; the fixes follow what it shows)
why (what they can do afterwards that they can't today): today nobody knows how
  long real callers wait for each answer on the VM. A slow agent sounds broken
  and people talk over it or hang up. First every turn gets measured, then the
  biggest delay gets cut.
acceptance criteria:
  - per turn: caller stopped speaking → text recognised → AI starts answering →
    first agent audio out, as milliseconds, logged as one `turn:` line and
    summarised per call (typical + slowest) into new call columns (needs
    IMP-013's migrations).
  - `/metrics`: reply time as a sum + count (like the greeting).
  - Then ONE targeted fix chosen from the numbers (e.g. end-of-speech wait
    0.6 s → lower, streaming check, model choice), each change measured
    before/after on real calls, not guessed.
test plan (offline): the per-turn arithmetic as a pure function; the timing
  hook with fake frames; columns written; metric exposed.
live check needed (on the VM): 3 calls with a few questions each, then the
  `turn:` lines and the per-call numbers. That shows where the time goes.
risk / blast radius: measurement only, until the chosen fix (which is its own
  commit with its own live check).
commit: on feature/multi-agent-pool (the commit titled `IMP-016: …`)
why rejected: —
Review notes:
  WHAT CHANGED
  - engine/turn_timing.py (new, small): keeps one call's reply times (count,
    median, slowest) and writes the `turn:` line.
  - engine/pipecat_engine.py: plugs in Pipecat's OWN latency observer rather
    than writing our own. It measures from when the caller actually stopped
    (it subtracts the voice detector's confirmation delay) to the agent's
    first audio. The worker now runs with `enable_metrics=True`, which is
    what lets each service report its own time and split a reply into:
    end of turn / AI first words / voice.
  - core/engine.py, core/records.py, stores/sqlite_store.py, bot.py: three new
    call columns (reply_turns, reply_median_s, reply_max_s). The IMP-013
    migrations add them to your existing database automatically.
  - core/live.py + api/server.py: `voiceagent_reply_seconds` on /metrics.
  - tests/test_reply_time.py (8), including one that drives Pipecat's REAL
    observer with frames and checks our handlers get its measurement.
  - docs: changelog, runbook troubleshooting row.
  CAUGHT BY A TEST: my first matcher filed speech-to-text's timing under
  "voice", because "DeepgramSTTService" contains the letters "TTS". Now it
  matches the full "TTSService" / "LLMService" names.
  ALREADY CHECKED HERE: 245 tests pass (237 + 8). Smoke on 18090/18091:
  clean, the reply metric is on /metrics, and start-up logged adding the 3 new
  columns (the IMP-013 system doing its job a second time). The silent test
  engine has no AI, so replies can only be measured on a real call.
  UNKNOWN UNTIL THE LIVE CALL: `enable_metrics=True` is new for the real
  engine. It should only add measurements, but watch for anything unusual.
  HOW TO TEST ON THE VM
  1. `git pull`, restart. The log shows `Records: added column calls.reply_...`
     three times (once only).
  2. Make 2–3 calls and ask 3–4 questions in each (short and long ones).
  3. Read the turn lines: `grep "turn:" logs/agent.jsonl | tail -12`
     (or watch the console). Each reply shows its total and its parts.
  4. Per call: python -c "import sqlite3; c=sqlite3.connect('records/calls.db'); print(c.execute('select started_at, reply_turns, reply_median_s, reply_max_s from calls order by started_at desc limit 3').fetchall())"
  5. **Send me the turn lines.** The biggest part decides the fix:
     - "end of turn" big → lower `turn_taking.silence_timeout_s` (now 0.6 s)
       and check the voice detector's stop delay;
     - "AI first words" big → the model (compare a lighter one);
     - "voice" big → text-to-speech settings.
  UNDO: `git revert <the IMP-016 commit>` (the new columns are harmless), or
  tag backup/2026-09-29-pre-IMP-016.

### IMP-017 — When nobody answers a transfer, the AI takes a message
status: APPROVED
kind: SLICE (backend focus #4)
for: caller, and whoever calls them back
source: today's "no one available" DIALSTATUS path plays a message and hangs up
size: M
why (what they can do afterwards that they can't today): if billing doesn't
  pick up, the caller hears "no one's free right now; can I take a message?",
  the agent collects name, callback number and reason, and the message is saved
  with the call. No lost leads.
acceptance criteria:
  - the dialplan's no-answer path sends the caller BACK into the AI (ARI
    continue into Stasis with a "take a message for <department>" marker)
    instead of hanging up; the dialplan change ships as paste-ready runbook text.
  - a `take_message` tool the LLM calls with name / number / reason; saved to a
    new `messages` table (department, call_id, created_at, done flag).
  - `GET /messages` (open messages, newest first) so the app can show them
    later; each also logged.
  - a caller who refuses or hangs up is fine: no half-saved message.
test plan (offline): the tool handler validates and saves; the messages table
  migration; the re-entry marker routes to message mode; `/messages`.
live check needed (on the VM): make the billing extension not answer (or turn
  the softphone off), call 6001, ask for billing: after the no-answer wait,
  the agent offers to take a message; give one; `curl localhost:8091/messages`
  shows it.
risk / blast radius: touches the Asterisk dialplan (off-repo) and ARI
  re-entry, the part of the system with the most live-only bugs (B-009, B-010,
  B-012). Expect a careful live check.
commit: —
why rejected: —
Review notes: —

### IMP-018 — Login on the server (the backend half of IMP-010)
status: APPROVED
kind: SLICE (backend focus #5)
for: supervisor
source: decisions 043 (no auth, so loopback-only); IMP-010
size: M
why (what they can do afterwards that they can't today): the API refuses
  anyone without a valid session, so it can later be opened to the office
  network. Everything in IMP-010's acceptance criteria EXCEPT the React login
  page: a scrypt password hash in .env set by `tools/set_password.py`, an HMAC
  cookie, POST /login + /logout (JSON), /health and /metrics open, a lockout
  after 5 tries, off unless configured. Until the login page exists, you log
  in with curl (documented), and the dashboard keeps working because login is
  off by default.
acceptance criteria / test plan: as IMP-010 (API parts), which stays for the
  page.
live check needed (on the VM): set the hash, restart, `curl /history` → 401;
  curl POST /login → cookie → `curl -b` /history → 200; /health still 200.
risk / blast radius: api/ + config only; off by default.
commit: —
why rejected: —
Review notes: —

### IMP-019 — Privacy: keep calls only as long as needed, and mask numbers
status: APPROVED
kind: SLICE (backend focus #6)
for: callers (their data), the business (legal risk)
source: roadmap §5 (no retention or access policy); IMP-015 (masking) folded in
size: M
why (what they can do afterwards that they can't today): transcripts and
  records holding callers' numbers and words are kept forever today. With this,
  they're deleted after a configured number of days, and numbers are masked in
  every API response by default.
acceptance criteria:
  - `service.retention_days` (no default guessed: the loop asks you for the
    number before building, because it's a business/legal choice). A daily
    cleanup deletes calls, turns, messages and recordings/ files older than
    that, off the event loop, logging counts.
  - IMP-015's masking, as written there.
  - `docs-vault/security-privacy.md` (roadmap §5 #5): what's stored, where, for
    how long, who can see it.
test plan (offline): cleanup on a temp DB + temp recordings dir (keeps young,
  deletes old, never touches outside recordings/); masking as IMP-015.
live check needed (on the VM): back up calls.db, set retention to a small
  number of days, restart: the log shows what was deleted; old calls are gone
  from the dashboard; numbers are masked.
risk / blast radius: deletes data by design. Hence the backup step, a dry-run
  mode logged first, and the number chosen by you.
commit: —
why rejected: —
Review notes: —
