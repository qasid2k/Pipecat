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
`APPROVED` (or `LIVE-TEST`). (`READY-FOR-REVIEW` is from the
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
  3. **Login** (IMP-010, proposed; decide IMP-009 first). One supervisor password (hash in `.env`, never in YAML), a
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
status: PARKED (2026-09-29, by request; no second LLM yet. Note: only the optional fallback_model needs one, and the apology + transfer works with one model)
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
commit: —
why rejected: —
Review notes: —

### IMP-009 — Decide: keep the web app as plain HTML, or adopt a frontend framework
status: PROPOSED
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
status: PROPOSED
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
  - `/login` (a small form page, same style) and `/logout`. Protected: `/`,
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
