# Supervisor web app: what goes on each page

**The information design for the web app**: which page holds what, and why.
The improvement loop builds web-app slices against this note. Visual mockup
(clickable, with notes per block):
https://claude.ai/artifact/XV6tzgYG2tRZMqroNqLD25 (private; open it while signed in), or
open the same page offline: `docs-vault/mockups/supervisor-blueprint.html`.

Related: [[product]] (theme 1), [[decisions]] 056 (React), 057 (Tailwind,
sidebar), [[backlog]] IMP-004.

---

## Principles (from research, 2026-09-29)

* **Real-time and history are separate pages.** Live answers "does anything
  need me right now?"; Insights answers "how are we doing over time?".
* **Few numbers on the live screen.** Every extra metric makes the rest less
  noticeable. Only numbers a supervisor would act on.
* **Every number drills down.** Clicking a figure on Live or Insights opens
  Calls, filtered to the calls behind it.
* **AI-specific measures come first.** The share of calls the AI handled
  without a person ("handled by the AI", also called containment), why
  calls went to a human, how long callers wait before the agent speaks, and
  errors.
* **Plain words.** "Handled by the AI", "Sent to a human", "Caller went
  quiet", not containment, escalation, idle timeout.

Sources: CustomerThink (supervisor screen KPIs), Voiso and Dialpad (real-time
dashboards), Hamming and Parloa (voice-agent analytics). Links are in the
mockup.

## Pages

| Page | Job | Holds |
|---|---|---|
| **Live** | In five seconds: does anything need me now? | Alert strip (only when something's wrong) · today's numbers: agents on call, calls today, handled by the AI %, sent to a human, problems · agent cards (status, caller, duration, the caller's latest line) · calls in progress · "Just now" feed |
| **Calls** | Find any finished call and understand it | Search + quick chips (today, sent to a human, problems, long) · list with an outcome chip and the caller's first line · detail: facts + why it was transferred, wait-before-greeting strip, conversation with inline events |
| **Insights** | How are we doing, and is it improving? | Today / 7 / 30 days · headline trends vs the previous period · calls per hour (AI vs human) · why calls went to a human (by department) · how calls ended |
| **Agents** | How is each agent doing? | Roster: status, voice, calls, average length, handled by AI %, sent to a human · agent detail (opening line, instructions, read-only) |
| **Settings** *(later)* | Run it without editing files | Agents & voices, instructions, departments, business hours, silence check-in, people & login. After login. |

## What data each needs

| Status | What |
|---|---|
| **Ready now** | Agent status and durations, live calls, filters, the conversation, transfers by department |
| **New query on stored data** | "Today" numbers, trends by period, calls per hour, per-agent stats, outcome grouping, the caller's first line |
| **New data to capture** | AI-model errors per call (alerts, "Just now" failures; pairs with IMP-008), per-call wait-before-greeting (IMP-002 timing, a small schema change), the transfer reason, call events with timestamps |
| **Later** | Search by what was said (text index), agent editing, Settings |

## Build order

1. **Insights page + "today" numbers on Live** (one summary endpoint over
   `calls`; numbers click through to Calls).
2. **Calls upgrade:** outcome chips, quick filters, the caller's first line;
   per-call timing stored for the strip.
3. **Agent stats.**
4. **Activity feed + alerts** (needs AI-error capture).
5. **Login → office-network access** (IMP-010, then slice 4).
6. **Settings.**
