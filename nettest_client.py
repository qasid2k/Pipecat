"""
Network-path isolation test -- run this on the ASTERISK SERVER.

Sends 320 bytes every 20ms (the exact same rate/chunk-size as our real audio)
to the laptop, for 60 seconds, with no AudioSocket protocol involved at all.
This tells us whether a plain TCP stream from THIS server to the laptop
survives, or stalls the same way our AudioSocket connection does.

Run:  python3 nettest_client.py <laptop-ip>
(get <laptop-ip> from the user's `.\\check-ip.ps1` output, or use whatever IP
is currently in the dialplan's AudioSocket() line)
"""
import socket
import sys
import time

if len(sys.argv) != 2:
    print("Usage: python3 nettest_client.py <laptop-ip>")
    sys.exit(1)

HOST = sys.argv[1]
PORT = 8091
CHUNK = bytes(320)
INTERVAL = 0.02  # 20ms, matching real audio frame pacing
DURATION = 60  # seconds

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.connect((HOST, PORT))
print(f"Connected to {HOST}:{PORT}. Sending 320 bytes every 20ms for {DURATION}s...")

start = time.monotonic()
last_report = start
sent = 0
next_send = start

try:
    while time.monotonic() - start < DURATION:
        sock.sendall(CHUNK)
        sent += len(CHUNK)
        now = time.monotonic()
        if now - last_report >= 1.0:
            print(f"t={now - start:5.1f}s  sent={sent:>9,} bytes  (no error so far)")
            last_report = now
        next_send += INTERVAL
        gap = next_send - time.monotonic()
        if gap > 0:
            time.sleep(gap)
except (BrokenPipeError, ConnectionResetError, OSError) as e:
    elapsed = time.monotonic() - start
    print(f"\n*** SEND FAILED after {elapsed:.1f}s, {sent:,} bytes sent: {e!r} ***")
    sys.exit(1)

print(f"\nDone. Successfully sent {sent:,} bytes over {DURATION}s with no error.")
sock.close()
