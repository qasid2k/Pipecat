---
description: One iteration of the supervised improvement loop — propose features, or build ONE approved item on its own branch
---

# /improve — one iteration of the improvement loop

You are running one iteration of this project's improvement loop. The design and
its reasons are in `docs-vault/decisions.md` 051 and `docs-vault/backlog.md`.
Read both before doing anything else.

**The shape of the loop:** you *propose* improvements, the human *approves* them
by editing `docs-vault/backlog.md`, you *build* approved ones on their own branch
and prove them with tests, and the human *merges* after a live call on the VM.
You never approve your own proposals and you never merge.

Do exactly ONE of the two modes below per iteration, then stop.

---

## HARD RULES — breaking any of these is a failed iteration

1. **Never merge, rebase, reset, push, or commit to the base branch.** The base
   branch is named on the `base:` line at the top of `docs-vault/backlog.md`.
   You may only create `improve/*` branches and `backup/*` tags, locally.
   Never `git push` anything, including tags.
2. **Never read, print or edit `.env`, `config.local.yaml` or
   `config.speech.yaml`.** Secrets and live settings live there.
3. **Never run anything that costs money or touches the live system:** no
   `tools/loadtest.py --real` / `--speech` / `--allow-real-engine`, no
   `bot.py` against the real engine, no calls to Deepgram, Gemini or the VM.
   The silent-engine load test is the only load test you may run.
4. **Never delete, skip or weaken an existing test.** The test count may only
   go up. If an existing test is genuinely wrong, mark the item BLOCKED and say
   why. That is a decision for the human.
5. **One item per iteration, at most ~400 changed lines.** If an approved item
   is bigger, do not build it: split it into smaller PROPOSED sub-items and
   mark the original BLOCKED ("split into IMP-…").
6. **`docs-vault/decisions.md` is append-only.** Supersede, never edit.
7. **Layering:** nothing outside `engine/` imports Pipecat; `core/` stays
   minimal. `tests/test_layering.py` enforces this, so do not "fix" it by widening
   its allow-list unless the item is explicitly about that.
8. **Never touch the user's work.** If pre-flight finds changes other than to
   `docs-vault/backlog.md` and `docs-vault/improvement-log.md`, stop.
9. **`backlog.md` and `improvement-log.md` are never committed by you.** They
   are the human's control surface and stay as working-tree edits on the base
   branch. When committing on an item branch, `git add` explicit paths only,
   never `-A` or `.`.
10. **No new dependencies** (nothing added to `requirements.txt`) unless the
    approved item says so explicitly. Tests use stdlib `unittest` only, not
    pytest.

Python is `.venv\Scripts\python.exe` (Windows) — never the system `py` (3.14 is
too new for Pipecat).
Test command: `.venv\Scripts\python.exe -m unittest discover -s tests -t .`
The test count is the `Ran N tests` line.

---

## STEP 0 — Pre-flight (always)

1. Read the `base:` line of `docs-vault/backlog.md`. Check that the current branch
   IS that base branch. If it is not, stop and report.
2. `git status --porcelain`. Anything other than the two control files → stop,
   log `REFUSED dirty tree`, and tell the user what is dirty.
3. Run the full test suite and record N (the baseline count). If it is red → stop and
   log `REFUSED baseline red`. Never build on a red base.
4. Look for an item with status `IN-PROGRESS`. That means a previous iteration died
   mid-build. If its branch exists and has commits, resume it at STEP B4. Otherwise
   set it back to `APPROVED` and continue.

## MODE A — PROPOSE (when no item is `APPROVED` or `IN-PROGRESS`)

1. Read: `docs-vault/roadmap.md` (§2 unfinished stages, §4 open questions,
   §5 doc gaps, and **§3 "deliberately not built" with its reopen triggers**),
   `docs-vault/bugs.md`, all of `docs-vault/backlog.md` (including REJECTED
   items), `docs-vault/knowledge-plan.md`, and the code relevant to each idea.
2. Think as the product owner of an **AI call-centre voice agent** (inbound
   calls on Asterisk, personas, transfer to departments, live dashboard, target
   500+ concurrent calls). Useful ideas make calls better (latency, barge-in, fewer
   dropped calls, clearer transfers), make operations safer (observability,
   rollback, config validation), or close a gap the roadmap already names.
3. Write **up to 3** new items, status `PROPOSED`, using the template in
   `backlog.md`. For each one:
   - Do not duplicate an existing item, and do not re-propose a REJECTED one
     unless you cite what has changed since.
   - A roadmap §3 item may only be proposed if its reopen trigger has fired,
     and you must quote the evidence.
   - Be honest about what offline tests CANNOT prove (audio quality, real
     provider behaviour, the dialplan). Fill in "live check needed".
   - Prefer small (S/M). An L item must come with its first S slice.
4. Append one line to `docs-vault/improvement-log.md`. Stop.

## MODE B — BUILD (the first `APPROVED` item, lowest number first)

1. **Backup:** `git tag backup/<YYYY-MM-DD>-pre-IMP-### <base>`.
2. Set the item's status to `IN-PROGRESS` in `backlog.md`, then
   `git switch -c improve/IMP-###-<short-slug>` from the base branch. (The
   backlog edit carries over as a working-tree change. Leave it uncommitted.)
3. **Tests first.** Write the tests that prove the acceptance criteria, run
   them, and see them FAIL for the right reason. Then write the code. Follow the
   surrounding code's style: its docstring density, and explaining *why*, not
   *what*.
4. **Gates, all required:**
   - full suite green, and `Ran N tests` ≥ baseline (it should be higher)
   - `tests/test_layering.py` green
   - if the item touches `transports/`, `core/pool.py`, `bot.py` or
     `engine/session_transport.py`: a silent-engine load-test smoke run
     (`tools/loadtest.py --write-config <scratch>/loop.yaml`, start `bot.py`
     with it in the background, `--spike 5 --duration 15`, stop the bot).
     It must report no dropped frames or pacer slips.
   If a gate fails, fix it. After **2** failed fix attempts: commit what exists
   to the branch with a `WIP:` message, set status `BLOCKED` with the exact
   failure, switch back to base, log it, and stop.
5. **Docs on the branch:** a `changelog.md` entry. A `decisions.md` entry (next
   number, append-only) if a design choice was made. Tick or answer the
   roadmap line if the item came from there.
6. Commit on the branch with explicit paths. End the message with the
   Co-Authored-By attribution line from the system reminder.
7. In `backlog.md`, set status `READY-FOR-REVIEW` and fill the item's
   **Review notes**. Write them for someone with basic Python and no telephony
   background:
   - what changed, file by file, in plain words
   - why it was done this way
   - how to test it on the VM (the exact live check), and what "working" looks like
   - how to roll it back (the backup tag name)
8. `git switch <base>`. Confirm `git log <base> -1` is unchanged from pre-flight.
   Append one line to `improvement-log.md`. Stop.

---

## Log line format (`docs-vault/improvement-log.md`)

`| <YYYY-MM-DD HH:MM> | PROPOSE/BUILD/REFUSED | IMP-### (or —) | tests <before>→<after> | <result in ≤12 words> |`

## End of iteration

Tell the user in 3–5 lines: which mode ran, which items changed status, what
needs them (approve or reject proposals, review a branch, a live check), and
anything that surprised you.
