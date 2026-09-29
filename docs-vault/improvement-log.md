# Improvement log

**One line per `/improve` iteration, append-only.** It shows whether the loop is
actually making the product better: the test count should climb, and REFUSED or
BLOCKED lines should be rare and explained. Related: [[backlog]],
[[decisions]] 051.

| When | Mode | Item | Tests | Result |
|---|---|---|---|---|
| 2026-09-28 10:40 | PROPOSE | IMP-001..003 | tests 131→131 | first proposals: silence re-prompt, greeting timing, Asterisk backup |
| 2026-09-28 11:30 | BUILD | IMP-001 | tests 131→147 | silence check-in built on branch, ready for live review |
| 2026-09-28 15:55 | BUILD | IMP-002 | tests 131→143 | greeting-delay breakdown built; smoke spike clean; ready for review |
| 2026-09-28 17:00 | VERIFIED | IMP-001 | tests 147 | live on VM: check-ins + goodbye work; tagged release/2026-09-28-IMP-001 |
| 2026-09-28 17:30 | BUILD | IMP-002 | tests 147→159 | moved onto working branch, conflicts merged; LIVE-TEST |
| 2026-09-28 17:50 | FIX | IMP-002 | tests 159→168 | added tools/check_greeting_timing.py live-check script |
| 2026-09-28 18:10 | PROPOSE | IMP-004..006 | tests 168→168 | web-app epic + call-history slice, setup doctor; IMP-002 still LIVE-TEST |
| 2026-09-28 18:20 | PARK | IMP-002, IMP-003 | tests 168 | parked by request; IMP-004 epic + IMP-005 approved |
| 2026-09-28 18:45 | BUILD | IMP-005 | tests 168→182 | recent-calls list + /history built, smoke clean; LIVE-TEST |
| 2026-09-29 09:00 | VERIFIED | IMP-005 | tests 182 | live on VM: recent calls work; tagged release/2026-09-29-IMP-005 |
| 2026-09-29 09:05 | PROPOSE | IMP-007, IMP-008 | tests 182 | web app slice 2 (detail+search); LLM-failure apology + transfer |
| 2026-09-29 09:20 | PARK | IMP-008 | tests 182 | parked by request; IMP-007 approved, build started |
| 2026-09-29 10:00 | BUILD | IMP-007 | tests 182→201 | call detail + filters built; isolated-port smoke clean; LIVE-TEST |
| 2026-09-29 11:00 | VERIFIED | IMP-007 | tests 201 | live on VM: detail + filters work; tagged release/2026-09-29-IMP-007 |
| 2026-09-29 11:05 | PROPOSE | IMP-009, IMP-010 | tests 201 | frontend decision (recommend plain) + login slice |
| 2026-09-29 11:20 | DECIDED | IMP-009 | tests 201 | option C: React + Vite + TS, build committed (decisions 056); IMP-011 port proposed |
