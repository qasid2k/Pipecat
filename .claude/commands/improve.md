---
description: One iteration of the improvement loop. Propose product-level work, or build the next approved item on the working branch, one feature at a time
---

# /improve: one iteration of the improvement loop

You are running one iteration of this project's improvement loop. Before doing
anything, read these three files:

- `docs-vault/product.md`: **what the product is becoming** and who it is for.
  It is the loop's north star.
- `docs-vault/backlog.md`: the work list and its statuses.
- `docs-vault/decisions.md` 051 and 053: why the loop works this way.

**The shape of the loop is linear, one feature at a time,** matching how this
project has always been built:

propose → human approves → build ONE item → commit + push to the working branch
→ **human tests it live on the VM** → human says it works → next item.

Nothing new is built while an item is waiting for its live test. You never
approve your own proposals.

Each iteration does exactly ONE of the modes below, then stops.

---

## HARD RULES: breaking any of these is a failed iteration

1. **One branch.** All work is committed to the working branch named on the
   `base:` line of `docs-vault/backlog.md`. Never commit to, merge into, or push
   `main`. Never force-push, rebase, or rewrite history. Undo is always a new
   `git revert` commit.
2. **Push only the working branch, only after the gates pass**
   (`git push origin <base>`). That is how the VM gets the code (`git pull`).
   Never push tags unless the human asks.
3. **Never read, print or edit `.env`, `config.local.yaml` or
   `config.speech.yaml`.** Secrets and live settings live there.
4. **Never run anything that costs money or touches the live system:** no
   `tools/loadtest.py --real` / `--speech` / `--allow-real-engine`, no `bot.py`
   against the real engine, no calls to Deepgram, Gemini or the VM. The
   silent-engine load test is the only load test you may run.
5. **Never delete, skip or weaken an existing test.** The test count may only
   rise. If an existing test is genuinely wrong, mark the item BLOCKED and say
   why.
6. **At most ~400 changed lines per build.** Big features are **epics** split
   into slices (see MODE A). An approved item that is too big gets split into
   PROPOSED slices, and the original is marked BLOCKED ("split into IMP-…").
7. **`docs-vault/decisions.md` is append-only.** Supersede, never edit.
8. **Layering:** nothing outside `engine/` imports Pipecat, and `core/` stays
   minimal. `tests/test_layering.py` enforces this. Don't "fix" a failure by
   widening its allow-list unless the item is explicitly about that.
9. **Never touch the human's other work.** If pre-flight finds changes to any
   file other than `docs-vault/backlog.md` and `docs-vault/improvement-log.md`,
   stop. A file whose `git diff` is empty (line endings only) doesn't count.
10. **No new dependencies or infrastructure** (packages in `requirements.txt`, a
    frontend framework, a build step, Docker, a database server) unless the
    approved item names it explicitly. Python tests use stdlib `unittest` only,
    not pytest. **The web app is the exception already decided** (decisions
    056): `web/` is React + Vite + TypeScript with Vitest. A NEW npm package
    still needs the approved item to name it, with a reason in the review
    notes.
12. **Frontend changes (`web/`):** run `npm test` and `npm run build` (in
    `web/`), and commit the rebuilt `api/static/` in the same commit as the
    source. The Python staleness test fails otherwise. Generated files
    (`package-lock.json`, `api/static/`) don't count toward the ~400-line
    limit and are never hand-edited.
11. **Explicit paths when staging** (`git add <file> …`), never `-A` or `.`.

Python is `.venv\Scripts\python.exe` (Windows). Never use the system `py`
(3.14 is too new for Pipecat).
Test command: `.venv\Scripts\python.exe -m unittest discover -s tests -t .`
The test count is the `Ran N tests` line.

---

## STEP 0: Pre-flight (always)

1. Check that the current branch is the `base:` branch. If not, stop and report.
2. Check `git status --porcelain` against rule 9. If anything else is dirty, stop,
   log `REFUSED dirty tree`, and say which file.
3. Run the full test suite and record N. If it is red, stop and log
   `REFUSED baseline red`.
4. **The gate: is any item in `LIVE-TEST`?** If so, an item is waiting for the
   human's VM check. **Build nothing.** You may still run MODE A if fewer than 3
   items are `PROPOSED`. Otherwise report "waiting for the live test of IMP-…"
   and stop.
   Items in `PARKED` were set aside by the human: they never gate the loop and
   are never built, even if their code is already committed. Items in
   `UNTESTED` are built and pushed but their live check is still owed: they
   don't gate the loop either, but list them in the end-of-iteration summary
   every time, so they aren't forgotten.
5. **An item still in `IN-PROGRESS`** means a previous iteration died
   mid-build. Look at `git log`: if its commit exists, go to B5; if not,
   discard its uncommitted changes to tracked files (only the files that item
   touched) and restart it at B1.

## MODE A: PROPOSE (nothing `APPROVED`, or an item is in `LIVE-TEST`)

Think as the **product owner** of the product described in `product.md`, not as
a code tidier.

1. Read `product.md` (themes, users, rules), `docs-vault/roadmap.md` (§2
   stages, §3 "deliberately not built" and its reopen triggers, §4, §5),
   `docs-vault/bugs.md`, all of `backlog.md` (including DONE and REJECTED
   items), and the code each idea touches.
2. Write **up to 3** new `PROPOSED` items. Each round must include **at least one
   item from product.md themes 1–3**: either a new EPIC with its first slice, or
   the next slice of an epic that already exists. Small fixes may fill the rest.
3. For each item:
   - **Say who it is for** (a row of product.md §2) and what they can do
     afterwards that they cannot do today.
   - **Epics:** write the epic as its own item (`kind: EPIC`) with an ordered
     slice list. Each slice is a separate `kind: SLICE` item (`epic: IMP-…`) that
     is at most ~400 lines, leaves the product working, and has its own live
     check. Slice 1 must be useful on its own. Only propose the NEXT slice as a
     buildable item; later slices stay as lines in the epic.
   - Don't duplicate an item, and don't re-propose a REJECTED one unless you
     cite what has changed.
   - A roadmap §3 item needs its trigger to have fired, and you must quote the
     evidence.
   - New infrastructure (a frontend framework, Docker, a database server) is its
     own decision item with the trade-offs. Never slip it into a feature.
   - Be honest about what offline tests CANNOT prove. Fill in
     "live check needed".
4. Append one line to `improvement-log.md`. Stop.

## MODE B: BUILD (the lowest-numbered `APPROVED` item, and nothing in `LIVE-TEST`)

An approved `EPIC` is never built itself. Build its lowest-numbered approved
`SLICE`.

1. **Backup tag:** `git tag backup/<YYYY-MM-DD>-pre-IMP-###` on the current
   commit. Set the item to `IN-PROGRESS` in `backlog.md`.
2. **Tests first.** Write tests for the acceptance criteria, run them, and see
   them FAIL for the right reason. Then write the code, following the
   surrounding style (docstrings that explain *why*, not *what*).
3. **Gates, all required:**
   - the full suite is green, and `Ran N tests` is above the baseline
   - `tests/test_layering.py` is green
   - if the item touches `transports/`, `core/pool.py`, `bot.py`, `api/` or
     `engine/session_transport.py`: a silent-engine smoke run **on its own
     ports**. The laptop's 8090/8091 may be a VS Code port forward to the LIVE
     VM; on 2026-09-29 a smoke run's API failed to bind and its requests went to
     the VM's real bot instead. So: generate the config **in the repo root**
     (prompt paths are relative to the config file) with
     `tools/loadtest.py --write-config config.loadtest.yaml` if it doesn't
     exist, copy it to `config.smoke.local.yaml` (gitignored) with
     `audiosocket_port: 18090` and api `port: 18091`, and start `bot.py
     config.smoke.local.yaml` in the background. Wait for
     `http://127.0.0.1:18091/health` and **check that it says
     `"engine": "silent"`**; if not, you are talking to something else, so stop.
     Run `tools/loadtest.py --port 18090 --api-port 18091 --spike 5 --duration 15`,
     exercise the new behaviour on 18091, then stop the bot and confirm
     18090/18091 are free. The run must show no dropped frames or pacer slips.
   After **2** failed fix attempts: run `git restore` on the files you changed,
   set the item to `BLOCKED` with the exact failure, log it, and stop. Commit
   nothing.
4. **Docs:** a `changelog.md` entry, a `decisions.md` entry (next number,
   append-only) if a design choice was made, and a tick or answer on the
   roadmap line if the item came from there.
5. **Review notes in `backlog.md`**, written for someone with basic Python and no
   telephony background:
   - what changed, file by file, in plain words
   - why it was done this way
   - **the live check on the VM**: `git pull`, restart `bot.py`, the exact
     calls to make, and what "working" looks like in the log
   - how to undo it: `git revert <sha>`, or the backup tag
   Set the status to `LIVE-TEST`.
6. **Commit** the item's files plus `backlog.md` and `improvement-log.md`
   together, with explicit paths. The message starts `IMP-###:` and ends with
   the Co-Authored-By attribution line from the system reminder. Then
   `git push origin <base>`.
7. Append one line to `improvement-log.md` (include it in the commit). Stop.

## MODE C: FOLLOW-UP (the human reports a live-test result)

- **"It works":** set the item to `DONE (live-verified <date>)`, tag the commit
  `release/<date>-IMP-###` (local), log it, and commit + push the backlog and
  log.
- **"It failed":** ask for the log lines if you don't have them. Diagnose
  first. Then either fix it as a new commit on the same item (it stays in
  `LIVE-TEST`, with the fix described in its notes), or, if the human wants
  it dropped, `git revert` it and set it to `REJECTED` with the reason.

---

## Log line format (`docs-vault/improvement-log.md`)

`| <YYYY-MM-DD HH:MM> | PROPOSE/BUILD/VERIFIED/FIX/REFUSED | IMP-### (or —) | tests <before>→<after> | <result in ≤12 words> |`

## End of iteration

Tell the user in 3–5 lines: which mode ran, which items changed status, **what
they need to do next** (approve, or run `git pull` on the VM and do the live
check), and anything that surprised you.
