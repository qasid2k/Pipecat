# Probes

Small standalone programs, each proving **one layer works in isolation**. None
of them is part of the running service; nothing imports them and they import
nothing from the repo.

They were written as incremental verification while the system was being built —
prove the network, then ARI, then audio through ARI, then the real thing. That
is also exactly the order to use them in when something breaks, which is why
they are kept rather than deleted.

## The ladder

Work **upwards**. The first one that fails is the layer at fault, and everything
above it is a waste of time until that is fixed.

| # | Probe | Proves | Reach for it when |
|---|---|---|---|
| 1 | `nettest_server.py` / `nettest_client.py` | raw TCP throughput between two hosts | audio is choppy or calls drop, and you suspect the network rather than the code |
| 2 | `ari_test.py` | ARI credentials, the WebSocket, and Stasis control — answers a call, waits, hangs up. **No audio.** | ARI errors at startup, transfers failing, `StasisStart` never arriving |
| 3 | `ari_media_test.py` | ARI **plus** External Media: bridges a call to an AudioSocket and echoes audio back | the caller hears silence, or `addChannel` is failing ([[bugs]] B-009, B-010) |
| 4 | `audiosocket_server.py` | the AudioSocket protocol on its own — a standalone echo server, no ARI, no Pipecat | the protocol framing or the I/O threads are suspect |

**A quicker route for most audio problems:** `engine.provider: silent` in the
real service does what probe 4 does, but through the actual transport, pool and
record path. Use these when you need to rule out a layer *below* that.

## Their history

Probe 4 is the ancestor of the whole service — `bot.py` grew out of it, and
`transports/audiosocket.py` is its protocol code with the threading model of
[[decisions]] 001 around it. It is superseded for serving calls and is kept only
as the smallest possible AudioSocket example.

Probes 1 and 2 earned their keep during the long "calls drop after a few
seconds" investigation ([[bugs]] B-001). The answer turned out to be the network
path between the laptop and the VM, not the code — which is a finding no amount
of staring at Python would have produced, and the reason a raw TCP probe is
worth keeping.

## Caveats

* They are **not tests.** No assertions, not in `tests/`, not run by
  `python -m unittest`. They print things for a human to read.
* They predate the current architecture and were not updated with it. Expect
  hardcoded hosts, ports and extension numbers — read before running.
* Probes 2 and 3 need `ARI_USER` / `ARI_PASSWORD` in `.env` and will talk to a
  real Asterisk. They answer and hang up real channels.
