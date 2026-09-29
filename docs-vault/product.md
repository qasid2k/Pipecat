# Product vision

**What this is becoming, and who it is for.** The improvement loop reads this
note before proposing anything ([[decisions]] 053), so its ideas serve the
product rather than just tidying the code. **This is a draft. Edit it freely**:
reorder the themes, strike things out, add your own. The loop follows whatever
is written here.

Related: [[roadmap]] (engineering stages), [[backlog]] (the work list),
[[knowledge-plan]].

---

## 1. The product in one paragraph

An **AI call centre that a business can switch on**. Inbound calls are answered
by named AI agents who hold a natural conversation, can answer from the
company's own knowledge, and hand callers to the right human team when needed.
Supervisors watch and review calls in a proper web app. A developer or IT
person can install it against their phone system in an afternoon, not a week.
Later it serves several companies from one deployment (SaaS).

## 2. Who uses it (every proposal should name one)

| Who | What they need | Today |
|---|---|---|
| **The caller** | a fast, natural, polite agent that understands them and gets them to the right place | works; 3–6 s of silence before the greeting; no knowledge of the company |
| **Supervisor / operator** | see live calls, find a past call, read or hear what happened, spot problems | a single read-only status page, loopback only, **no login**, no call history, no search |
| **Business admin** | change agents, prompts, greeting, departments and hours **without editing YAML** | edit `config.yaml` + prompt files by hand, then restart |
| **Developer / installer** | install, connect it to Asterisk (or another carrier), check it works, upgrade safely | many manual steps in [[runbook]] §1–4; dialplan pasted by hand; the Asterisk config lives only on the VM |

## 3. Themes, roughly in priority order

Each theme is a direction, not a task. The loop turns them into **epics**
(multi-step features) and then into slices small enough to build and live-test
one at a time.

1. **Supervisor web app.** *(React + Tailwind, sidebar + pages; decisions 056,
   057. The UI itself is part of the product: every slice improves how it
   looks and feels, not just what it shows. What goes on which page:
   [[web-app-design]].)* Replace the status page with a
   real frontend: a
   login, live calls, **call history with search and filters**, a call detail
   view (transcript, persona, cause, transfer, timings), and agent/persona
   status. **Authentication comes first**, because `/calls` exposes caller
   numbers ([[decisions]] 043). The data already exists in `records/calls.db`
   and `/api`.
2. **Easy setup for developers.** One-command install (a script or Docker), a
   `doctor` / pre-flight command that checks keys, ports, ARI and the dialplan
   and says what is wrong in plain words, a generated or templated Asterisk
   config, and a first-call walkthrough. Includes backing up the Asterisk
   config (IMP-003).
3. **Admin without YAML.** Manage personas, prompts, greetings, departments and
   check-in wording from the web app, validated by the same rules as
   `core/config.py`, applied without dropping live calls.
4. **Better conversations.** Company knowledge ([[knowledge-plan]], parked
   until the content exists), a shorter wait before the greeting (IMP-002
   measures it), business hours and after-hours handling, taking a message
   when no human is free.
5. **Operability at scale.** The [[roadmap]] stages E–G: real-engine capacity,
   horizontal scale, alerting, recording with a privacy policy.
6. **Multi-tenant / SaaS readiness.** `tenant_id` is already threaded through.
   Per-tenant config, isolation, usage metering. Later, and only once 1–3 exist.

## 4. Rules the loop keeps to when thinking big

* **An epic is a list of slices, not one giant change.** Each slice is at most
  ~400 changed lines, leaves the product working, and has its own live check.
  Slice 1 must be useful on its own.
* **Every proposal names who it is for** (the table in §2) and what they can do
  afterwards that they cannot do today.
* **Mix sizes.** Each proposal round includes at least one item from themes 1–3
  (an epic or its next slice) as well as any small fixes.
* **No new infrastructure without a reason written down**: a frontend
  framework, a build step, a database server, Docker. Each is a real cost for a
  small team, and [[decisions]] has examples of choosing the simpler thing on
  purpose (SQLite over Postgres, one HTML file with no CDN). Propose it as its
  own decision item and let the human choose.
* **[[roadmap]] §3 "deliberately not built" still applies.** A theme here does
  not override a reopen trigger that has not fired.
