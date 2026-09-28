# Improvement backlog

base: feature/multi-agent-pool

**The work list of the improvement loop ([[decisions]] 051).** `/improve` reads
this file, adds proposals to it, and builds only the items marked `APPROVED`.
Related: [[roadmap]], [[improvement-log]], [[runbook]] §8.

> The `base:` line above is the branch every item is built from. Change it when
> the working branch changes (e.g. to `main` after this branch is merged).

---

## How to use it (the human's part of the loop)

| You want to… | Do this |
|---|---|
| accept an idea | change `status: PROPOSED` → `status: APPROVED` |
| turn one down | change it to `status: REJECTED` and write a reason on the `why rejected:` line. **Keep the item.** A deleted rejection gets proposed again. |
| review a built item | `git diff <base>..improve/IMP-###-…`, read its **Review notes**, do the live check on the VM |
| accept a built item | merge the branch yourself, tag the base `release/<date>`, set `status: MERGED` |
| undo one | see [[runbook]] §8 |

Commit this file yourself whenever you like. The loop never commits it.

### Status meanings
`PROPOSED` written by the loop, waiting for you · `APPROVED` you said yes, the next
`/improve` builds it · `IN-PROGRESS` being built (if it stays here, an iteration
died, and the next one resumes it) · `READY-FOR-REVIEW` built and tested on its
branch, waiting for your review · `MERGED` you merged it · `REJECTED` you said no ·
`BLOCKED` the loop could not finish it, and the reason is on the item

### Item template
```
### IMP-### — <title>
status: PROPOSED
source: roadmap §x #y | bugs B-0xx | agent idea
size: S | M | L
why (value for the call centre):
acceptance criteria:
test plan (offline):
live check needed (on the VM):
risk / blast radius:
branch: —
why rejected: —
Review notes: —
```

---

## Items

*(none yet. Run `/improve` for the first proposals)*
