# Bugs

One entry per bug: **symptom → root cause → fix**. These are all *already fixed*;
the value is that the symptom is searchable and the fix is not re-litigated.
The earlier entries were recovered from code comments and git history on
2026-07-30, so their dates are approximate.

Related: [[architecture]], [[decisions]], [[runbook]].

---

## B-001 — Calls dropped with "Resource temporarily unavailable"
**Symptom.** Asterisk hangs up seconds into a call. Asterisk logs
`Resource temporarily unavailable` or a connection reset; our side sees a write
error or the socket closing with no HANGUP message.

**Root cause.** Two compounding causes.
1. Socket I/O on the asyncio event loop. Any stall (ONNX model load, GC, LLM/TTS
   inference) meant we did not drain Asterisk fast enough. AudioSocket's writer
   is non-blocking with zero tolerance and simply gives up.
2. The receive buffer was enlarged on the **accepted** socket, per connection.
   Too late: TCP negotiates its **window-scaling factor during the handshake**,
   from the *listening* socket's buffer size. With the scale already fixed small,
   the effective receive window stayed ~64 KB no matter how much buffer memory
   we allocated afterwards.

**Fix.** (1) Move socket I/O to dedicated blocking threads — see [[decisions]]
001. (2) Set `SO_RCVBUF = 1 MB` on the **listening** socket **before `bind()`**
(`make_listen_socket`, `bot.py:454`), so every accepted connection negotiates a
large window from its first packet. The per-connection `setsockopt` is kept as
belt-and-braces.

**Do not regress.** Never move socket reads onto the event loop, and never move
the `SO_RCVBUF` call after `bind()`.

---

## B-002 — Agent's speech played back slow and choppy (Windows only)
**Symptom.** On the Windows dev laptop the agent sounded dragged out and
stuttery; audio arrived ~1.6× slower than real time.

**Root cause.** Windows' default timer granularity is ~15.6 ms, so a 20 ms
`time.sleep()` / queue timeout rounds up to ~31 ms. Every frame was paced at
31 ms instead of 20 ms.

**Fix.** Request 1 ms timer resolution at import time on `win32`
(`ctypes.windll.winmm.timeBeginPeriod(1)`), and pace off `time.monotonic()`
rather than relying on the queue timeout
(`audiosocket_transport.py:73`, `_write_loop`).

---

## B-003 — `WinError 10013` on bind
**Symptom.** The bot could not start on Windows; `bind()` raised
`OSError WinError 10013` (an attempt was made to access a socket in a way
forbidden by its access permissions).

**Root cause.** `SO_REUSEADDR` was set on the listening socket. On Windows that
option means something different and hostile compared to POSIX; asyncio itself
skips it on Windows for the same reason.

**Fix.** Do not set `SO_REUSEADDR` (noted in a comment at `bot.py:466` so nobody
"helpfully" adds it back).

---

## B-004 — Transfer announced but never happened
**Symptom.** The caller asks for a human. The agent says "let me connect you"…
and then just keeps talking. No transfer.

**Root cause.** The LLM treated the transfer as something to *narrate* rather
than a tool to *call*. It produced the sentence and never emitted the function
call. Nothing was wrong with the ARI code.

**Fix.** Prompt engineering, in the `# TRANSFERS` block of `SYSTEM_PROMPT`:
"**CALL** the `transfer_to_department` tool. Actually call the tool; do not just
say you will. Calling the tool is the ONLY thing that connects them, and it
already tells the caller you're connecting them."

**Lesson.** With tool-using LLMs, "the feature does not work" is often a prompt
bug. Check whether the tool was even invoked before debugging the tool.

---

## B-005 — Caller cut off mid-sentence during transfer
**Symptom.** The transfer worked, but the caller was chopped off in the middle of
the agent's "connecting you now", making it feel like a dropped call.

**Root cause.** `channels/{id}/continue` takes the channel out of Stasis, which
drops the External Media leg and kills the audio path immediately — before the
TTS audio had finished playing out.

**Fix.** The tool handler returns to the LLM straight away and performs the ARI
call in a background task after `await asyncio.sleep(3.0)`
(`transfer_to_department`, `bot.py:176`). See [[decisions]] 008 for why this is
a fixed sleep and what would be better.

---

## B-006 — Two calls in the same second overwrote each other's transcript
**Symptom.** With concurrent calls, one call's transcript/conversation files were
missing or contained a mixture of two calls.

**Root cause.** The filename stamp was `%Y%m%d-%H%M%S` only. Two calls connecting
within the same second produced the identical filename.

**Fix.** Append 6 hex characters of a `uuid4` to the stamp
(`handle_call`, `bot.py:381`). Fixed in commit `d751590`.

---

## B-007 — `save_conversation` crashed after a tool call
**Symptom.** Any call in which `transfer_to_department` ran ended with
`Could not save conversation: 'LLMSpecificMessage' object has no attribute 'get'`
— so exactly the interesting calls lost their conversation JSON.

**Root cause.** `LLMContext.messages` is not homogeneous. Normal turns are plain
dicts, but once a tool runs, provider-specific `LLMSpecificMessage` objects are
appended. The save filtered with `m.get("role")`, which those objects do not have.

**Fix.** `_message_to_record()` (`bot.py:326`) normalises every message: dicts
pass through, `LLMSpecificMessage` is unwrapped via `.message`, and anything else
is stringified with a best-effort `role` so the dump can never fail. Fixed in
commit `519cd29`.

---

## B-008 — Gemini started returning 404
**Symptom.** Every LLM call failed; the model name was rejected.

**Root cause.** The pinned `gemini-2.5-flash` was retired by Google.

**Fix.** Use the `-latest` alias — `gemini-flash-lite-latest`. See [[decisions]]
004 for the accepted trade-off (Google may change the model under us).

---

## B-009 — Our own External Media channel was treated as a new call
**Symptom.** Spurious extra "call" handling; a second bridge/media channel being
created for a channel that was really our own.

**Root cause.** An External Media channel created with `app=<our app>` also
raises `StasisStart`, so the handler saw it as an incoming caller.

**Fix.** Track the ids we generate in `self._em_ids` and return early from
`_on_stasis_start` for any channel in that set (`ari_controller.py:109`).

---

## B-010 — `addChannel` 422 "Channel not in Stasis application" ✅ FIXED
**Observed 2026-07-30, fixed the same day.** Intermittent; the call it was first
seen on worked fine (greeting, conversation, transfer and the two-concurrent-call
test all passed), so it was a latent fault rather than a live outage.

**Symptom.**
```
ARI POST /bridges/{id}/addChannel -> 422: {"message": "Channel not in Stasis application"}
ARI: <chan> bridged into the AI pipeline (uuid=...)      <-- logged anyway
```
The second line is printed **unconditionally** right after the failing call, so
the log claims success it did not get.

**Root cause.** A race on the External Media channel.
`POST /channels/externalMedia` returns as soon as Asterisk has *created* the
channel, but the channel only *enters* the Stasis app a moment later —
asynchronously, announced by its own `StasisStart` event. `addChannel` requires
it to be in Stasis already, so an `addChannel` issued immediately can arrive a
few milliseconds too early. It usually wins the race, which is why this was
never noticed while the demo was stable.

Two things hide it: `_on_stasis_start` ignores what `_add()` returns, and the
success message above is not conditional on it.

**Why it matters.** When the race *is* lost for real, the media leg never joins
the bridge, so there is no audio path in either direction: the caller hears
silence, `in=0`, and the call dies at the 30 s idle timeout. Indistinguishable
in the logs from several other faults — which is the actual cost of the lying
success message.

**Not caused by the Phase 2 refactor.** Diffed against the demo-stable commit
`519cd29`: the only change to `ari_controller.py` was the new `caller_id` field.
The sequence of ARI calls is byte-identical.

**Fix.** Wait for the EM channel's own `StasisStart` before bridging it. Its
event used to be discarded by an early `return`; it now opens an
`asyncio.Event` gate (`_em_ready[em_id]`) that setup awaits, with a 2 s
`EM_STASIS_TIMEOUT_S` safety net. `_add()`'s result is now checked, and on real
failure the call is torn down with a loud error instead of being reported as
healthy.

> **Trap:** you cannot `await` that gate inside `_on_stasis_start` as it was
> written. The ARI WebSocket loop handled messages one at a time
> (`await self._on_stasis_start(event)`), so blocking there means the EM
> channel's `StasisStart` can never be read — a guaranteed deadlock for the full
> timeout, on every call. Handlers are therefore dispatched as tasks
> (`_dispatch`/`_spawn`), while the EM `StasisStart` is handled inline because
> all it does is open the gate.

### Three further bugs this fix flushed out

**(a) `_req()` returned `None` for both failure and success-with-no-body.**
`addChannel`, `answer`, `hangup` and `continue` all answer `204 No Content`, so
the new "did the add work?" check would have treated **every successful call as a
failure** and torn down every call. `_req` now returns `True` for a 2xx with an
empty body, and `None` only on failure. Any future code that checks an ARI result
depends on this distinction.

**(b) Phantom calls from our own media channel.** Making handlers concurrent
opened a window the old serialized code could not hit: if the caller hangs up
*during* setup, teardown ran and discarded the EM id — and then that channel's
still-in-flight `StasisStart` was no longer recognised as ours, so it was handled
as a **new incoming call**: answering our own media channel, building a second
bridge and creating yet another media channel for it. Fix: teardown drops only
the gate (`_release_gate`); the id is retired in `_on_stasis_end`, which is the
channel's genuinely final event. `_forget_em` is now reserved for channels that
were never created, where no events can follow.

**(c) Pointless bridging after teardown.** After the gate wait, setup now checks
that its registry entry still exists and abandons bridging if the call went away.

Known, accepted edge case: a media channel that is created but never enters
Stasis leaves one short id string in `_em_ids` for the process lifetime, because
no `StasisEnd` ever arrives to retire it. Bounded and tiny; preferable to
re-opening the phantom-call window.

**Verified** with an event-ordering harness (fake ARI events + stubbed REST):
28/28 checks — including delayed `StasisStart`, a genuinely failing add, the
never-enters-Stasis timeout, hangup-during-setup with no phantom call and no
orphaned bridge, and two concurrent setups not crossing caller ids or media
channels.
