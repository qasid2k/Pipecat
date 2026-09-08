# Adapters

**The contract for putting a new telephony service in front of this agent.**

Onboarding a vendor should be a contained job: write one module, satisfy two
interfaces, change one line of `config.yaml`. Nothing in `core/`, `engine/`,
`bot.py` or `core/pool.py` should need to change. If you find yourself editing
those, the adapter is doing something the contract did not anticipate — say so
rather than working around it.

Related: [[flow]], [[architecture]], [[decisions]], [[runbook]], [[roadmap]].

---

## The honest status of this contract

It has been exercised by **exactly one vendor**. Asterisk is a reference
implementation, not a proof — the shape below is an informed design, and the
first genuinely different vendor will probably find one or two places where it
bends. That is expected. What matters is that the seams are in the right places
and the core does not depend on the adapter.

`TwilioTransport` is referred to in older notes as a second reference
implementation. **It was designed but never built** ([[decisions]] 025) — there
was no account to verify it against, and an unverified adapter proves nothing
while still costing maintenance. Treat its design notes in [[runbook]] §7 as a
sketch, not a worked example.

---

## What you implement

Two abstract classes in `core/transport.py`. Python refuses to instantiate a
subclass that misses a method, so the contract is enforced at runtime.

### `BaseTransport` — one connection to a vendor

| Method | Must do |
|---|---|
| `async start()` | Begin accepting calls: bind sockets, connect to the vendor's API. |
| `async stop()` | Stop accepting and release vendor resources. **Idempotent.** |
| `listen() -> AsyncIterator[CallSession]` | Yield one session per incoming call. |
| `async reject(call)` | Refuse a call we will not serve (at capacity). |

Two rules on `listen()` that are easy to get wrong:

* **Do no slow per-call setup inline.** Yielding is a serialisation point: work
  done in the loop makes call N+1 wait behind call N. Do it in a task and yield
  the session once it is ready. Asterisk's adapter waits up to 2 s for a
  correlation UUID this way (`transports/asterisk.py` `_intake`).
* **Audio is already flowing when the vendor connects.** The clock is running
  before you yield.

`reject()` is separate from `hangup()` because vendors distinguish "no" from
"goodbye" — and because for a vendor without a dialplan it is the *only* place
capacity can be refused (see Capacity, below).

### `CallSession` — one live call

| Member | Must do |
|---|---|
| `call_id` | Stable id for this call. Used in every log line and record. |
| `caller_id` | The caller's number, or `"unknown"`. Never trust it — it is caller-supplied on most trunks. |
| `ended` | An `asyncio.Event` set when the call is over, from either side. |
| `end_reason` | Human-readable, set by the adapter at the moment the call ends. |
| `async read_audio()` | Next frame of caller audio, or **`None` exactly once** when the call has ended. That `None` is the sentinel the engine's read loop stops on. |
| `async write_audio(pcm)` | Send one frame of agent audio. **May block asynchronously** — that back-pressure is what paces speech to real time. |
| `async transfer(dest)` | Hand the call to `dest`. Returns `True` if *initiated*, not if a human answered. |
| `async hangup()` | Release this call's resources. **Must be safe to call twice.** |
| `can_transfer` | Whether `transfer()` can work on this call, known up front. |

---

## The three contracts an adapter must satisfy

### 1. Audio: convert inside the adapter

Every byte crossing this interface is **8 kHz, 16-bit signed, mono PCM
("slin"), in 20 ms / 320-byte frames**. The constants are in
`core/transport.py` (`CANONICAL_SAMPLE_RATE` and friends) — import them, do not
re-declare 8000 and hope everyone agrees.

If your vendor speaks something else — Twilio speaks 8 kHz mu-law, base64, over
a WebSocket — **convert it inside your adapter**. The core and the engine must
never see a vendor format. Two traps, both already paid for:

* **Do not reuse Pipecat's serializers** to do the conversion. That would make
  `transports/` import Pipecat and undo the engine seam entirely
  ([[decisions]] 018).
* **Do not use `audioop`.** Deprecated, and removed in Python 3.13 — leaning on
  it silently caps the project's Python version. G.711 is about forty lines and
  two lookup tables, and can be proven bit-exact against `audioop` as a
  *test-only* oracle.

### 2. Control: transfer and reject in the vendor's own terms

The interface hides the difference; each adapter implements it natively.

| | Asterisk | A vendor with no dialplan |
|---|---|---|
| Transfer | ARI `POST /channels/<id>/continue` into the `[transfer]` context; the department name *is* the extension | a REST redirect / new call instructions |
| Reject | close the audio path; the dialplan already played the busy message | **answer and say something yourself** — nothing else will |

`transfer()` returning `True` means handed over, not answered. What happens next
is the vendor's business.

`hangup()` deliberately does **not** end the caller's channel on Asterisk: it
runs in `run_call`'s `finally`, which also fires after a successful transfer,
where the call now belongs to a human. Whatever your vendor's equivalent is,
make sure `hangup()` cannot destroy a call you have already handed away.

### 3. Capacity: the app-side gate is the only universal one

There are two gates ([[decisions]] 031):

* **`pool.acquire()` returns `None` → `transport.reject(call)`.** Transport-
  agnostic, always present, and for a vendor without a dialplan the **only**
  gate.
* **The Asterisk dialplan `GROUP_COUNT` cap** is Asterisk-only. It exists purely
  so the caller hears a spoken message before reaching the app.

A new adapter gets the first for free and should not try to reimplement the
second. It should make `reject()` do something a caller can understand.

---

## Threading and the event loop

Read [[decisions]] 001 before choosing an I/O model. The Asterisk adapter uses
two blocking OS threads per call rather than asyncio sockets, because AudioSocket
is lockstep with zero tolerance: a frame handed over late loses the call. Any
stall on the event loop — a model load, GC, an import — has already cost this
project two outages ([[bugs]] B-001, B-011).

What that means for you:

* **Never block the event loop** in an adapter. If your vendor's client library
  is synchronous, thread it.
* `pool.acquire()` / `pool.release()` are **event-loop only**. `asyncio.Lock` is
  not thread-safe; calling them from an I/O thread corrupts the pool in a way
  that looks like a random, unreproducible double-booking.
* If your `write_audio()` does not get pacing for free from vendor back-pressure
  (Asterisk does; a vendor that buffers whatever you send does not), **pace it
  yourself off a monotonic clock**. Without that, barge-in breaks: the agent's
  audio is already buffered downstream when the caller interrupts.

---

## Wiring it up

1. Write `transports/<vendor>.py` with your `BaseTransport` and `CallSession`.
2. Add a branch to `create_transport()` in `factories.py`. Import the module
   **inside** the branch — a lazily imported vendor means its dependencies never
   have to be installed by someone not using it, and a broken adapter cannot
   stop the others loading.
3. Add the provider name to `VALID_TRANSPORTS` in `core/config.py` and a config
   section for its settings. Secrets are named, never held: `api_key_env:`.
4. Add tests. `tests/test_call_loop.py` shows how to drive the call loop with a
   fake session and no live vendor.

`tests/test_layering.py` enforces that your adapter does not import Pipecat and
that `core/` stays clean. It runs on every test invocation.

---

## Capability checklist

Before calling an adapter done:

- [ ] Audio converted to/from canonical 8 kHz / 16-bit / mono inside the adapter
- [ ] `read_audio()` returns `None` exactly once at end of call
- [ ] `write_audio()` applies back-pressure or paces itself
- [ ] `hangup()` is idempotent and cannot kill a transferred call
- [ ] `can_transfer` is honest — the agent tells the caller the truth up front
- [ ] `reject()` does something the caller can understand
- [ ] `stop()` is idempotent and releases everything
- [ ] `end_reason` is set on every path
- [ ] `call_id` is stable and unique
- [ ] Nothing blocks the event loop; nothing touches the pool off-loop
- [ ] No Pipecat import (`tests/test_layering.py` proves it)
- [ ] The full smoke test passes: [[runbook]] §5, steps 1–10

## Reference implementation

`transports/asterisk.py` — `AsteriskTransport` + `AsteriskCallSession`, with
`transports/audiosocket.py` for the protocol and `ari_controller.py` for control.
[[flow]] §3 walks through exactly how a call arrives through it, including the
UUID correlation that joins the control and media halves.
