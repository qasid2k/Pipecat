# Supervisor web app: what goes on each page

**The information design for the web app**: which page holds what, and why.
The improvement loop builds web-app slices against this note. Visual mockup
(clickable, with notes per block):
https://claude.ai/artifact/XV6tzgYG2tRZMqroNqLD25 (private; open it while signed in), or
open the same page offline: `docs-vault/mockups/supervisor-blueprint.html`.

Related: [[product]] (theme 1), [[decisions]] 056 (React), 057 (Tailwind,
sidebar), [[backlog]] IMP-004.

*v2, 2026-09-29: revised after a design review of v1 (see "Review fixes").*

---

## Principles (from research, 2026-09-29)

* **Real-time and history are separate pages.** Live answers "does anything
  need me right now?"; Insights answers "how are we doing over time?".
* **Few numbers on the live screen.** Every extra metric makes the rest less
  noticeable. Only numbers a supervisor would act on.
* **Every number drills down.** Clicking a figure on Live or Insights opens
  Calls, filtered to the calls behind it.
* **AI-specific measures come first**, and they have to be honest: the share
  of calls the AI actually resolved, why calls went to a human, callers who
  couldn't get through, and errors.
* **Supervisors and operators are different audiences.** Caller-facing numbers
  go on the supervisor pages; server numbers (provider speed, greeting wait,
  audio frames, CPU) go on System health.
* **Private by default.** Caller numbers are masked unless a logged-in
  supervisor is looking; a wallboard never shows what callers said.
* **Plain words.** "Resolved by the AI", "Sent to a human", "Couldn't get
  through", "Unclear", not containment, escalation, abandonment.

Sources: CustomerThink (supervisor screen KPIs), Voiso and Dialpad (real-time
dashboards), Hamming and Parloa (voice-agent analytics). Links are in the
mockup.

## Definitions every page uses

* **Resolved by the AI**: not transferred, not failed, AND it lasted more than
  a few seconds, the caller didn't hang up while the agent was speaking, and
  the same number didn't call again within 24 hours. A call that fails those
  checks is **Unclear** (with the reason), never counted as resolved. This
  guards against "false containment": a caller who gave up is not a success.
* **Couldn't get through**: rejected by the dialplan's busy gate before it
  reached the bot ([[decisions]] 041). Only Asterisk's call records (CDR) know
  about these callers; the bot has no row for them.
* **Today / this week**: in the configured **business time zone**, not the
  server's UTC clock.

## Pages

| Page | For | Job | Holds |
|---|---|---|---|
| **Live** | supervisor | In five seconds: does anything need me, and can I act on it? | Supervisor / Wallboard switch · alert strip (only when something's wrong) · today: agents on call, calls (answered + missed), resolved by the AI %, sent to a human, problems (couldn't get through + failed) · agents as cards or a table (status, masked caller, duration, latest caller line, **Listen in / Transfer**) · calls in progress · "Just now" feed |
| **Calls** | supervisor | Find any finished call and understand it | Search + quick chips · list with a plain outcome per call and the caller's first line · **Export CSV** · detail: facts + why it was transferred, wait-before-greeting strip, conversation with inline events |
| **Insights** | supervisor, manager | How are we doing, and is it improving? | Today / 7 / 30 days · trends vs the previous period (resolved, sent to a human, couldn't get through, average call) · calls per hour · why calls went to a human · how calls ended · weekly email summary |
| **Agents** | supervisor | How is each agent doing? | Sortable roster: status, voice, calls, average length, resolved %, unclear, sent to a human · agent detail (read-only) |
| **System health** | admin, developer | Is the platform healthy, and what's to blame when calls are slow? | AI providers (typical and slowest response, errors) · wait before greeting (typical, slowest 10%, by step) · audio frames · machines (calls, CPU, memory, version) |
| **Settings** *(later)* | admin | Run it without editing files | Agents & voices, instructions, departments, business hours & time zone, alerts & notifications, privacy (masking, access, retention), silence check-in, people & login. After login. |

## What data each needs

| Status | What |
|---|---|
| **Ready now** | Agent status and durations, live calls, filters, the conversation, transfers by department, audio frame counters, CPU/memory (Linux) |
| **New rules or queries on stored data** | Resolution rule + "unclear", business time zone, masking, "today" numbers, trends, calls per hour, per-agent stats, outcome grouping, caller's first line, CSV export |
| **New data to capture** | Couldn't-get-through callers (CDR import) · AI-model errors and provider response times (pairs with IMP-008) · per-call wait-before-greeting (IMP-002 timing, small schema change) · transfer reason · call events with timestamps · running version |
| **Later** | Search by what was said (text index), topic summaries (an AI call per call, costs money), Listen in / Transfer (after login), agent editing, Settings, notifications |

## Build order

1. **Honest numbers**: resolution rule + "unclear", business time zone,
   masked numbers by default.
2. **Insights page + today's numbers on Live** (one summary endpoint over
   `calls`; numbers click through to Calls).
3. **Couldn't get through** (CDR import into Live, Calls, Insights).
4. **Calls upgrade + CSV export.**
5. **System health** (provider timing/errors, greeting wait, audio,
   machines); engineering numbers leave the supervisor pages.
6. **Agents: stats + table view for large teams.**
7. **Login → office-network access** (IMP-010, then slice 4): unlocks full
   numbers, Wallboard mode and actions.
8. **Listen in / Transfer** from Live.
9. **Settings + notifications** (email/Slack alerts, weekly summary, privacy).

## Review fixes (v1 → v2)

| Shortcoming in v1 | Fix |
|---|---|
| "Handled by the AI" counted callers who gave up | Stricter "Resolved by the AI" + an "Unclear" bucket |
| Busy callers rejected by the dialplan were invisible | "Couldn't get through" from Asterisk CDR |
| Full numbers and caller words on a shareable screen | Masked by default; Wallboard mode; full detail after login |
| Supervisor could watch but not act | Listen in / Transfer on agent cards (after login) |
| Cards don't scale to 50 agents | Cards or table, table sorted by what needs attention |
| Assumed one bot machine | Pages read one shared source; System health lists machines |
| Alerts only on screen | Email/Slack notifications (Settings) |
| "Today" undefined (UTC) | Business time zone setting |
| Engineering metrics on the supervisor page | Moved to System health |
| No way to get data out | CSV export; weekly email summary |
