"""
Network-path isolation test -- run this on the LAPTOP.

Just accepts one TCP connection and counts bytes received over time, with no
AudioSocket protocol, no Asterisk, no Pipecat involved at all. If a plain TCP
stream from the Asterisk server ALSO stalls around ~4 seconds, the problem is
the network path (VPN/router/NAT), not our application code.

Run:  .venv\\Scripts\\python.exe nettest_server.py
Then on the Asterisk server, run the paired nettest_client.py against this
laptop's current IP (check with .\\check-ip.ps1) on port 8091.
"""
import socket
import time

HOST = "0.0.0.0"
PORT = 8091

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.bind((HOST, PORT))
srv.listen(1)
print(f"Listening on {HOST}:{PORT} -- waiting for the server-side test to connect...")

conn, addr = srv.accept()
print(f"Connected from {addr}")

start = time.monotonic()
last_report = start
total = 0
last_total = 0

try:
    while True:
        data = conn.recv(65536)
        if not data:
            break
        total += len(data)
        now = time.monotonic()
        if now - last_report >= 1.0:
            delta = total - last_total
            print(
                f"t={now - start:5.1f}s  total={total:>9,} bytes  "
                f"+{delta:>6,} in last {now - last_report:.1f}s"
                + ("  <-- STALLED" if delta == 0 else "")
            )
            last_report = now
            last_total = total
except (ConnectionResetError, OSError) as e:
    print(f"\n*** CONNECTION ERROR: {e!r} ***")
finally:
    elapsed = time.monotonic() - start
    print(f"\nDone. Ran for {elapsed:.1f}s, received {total:,} bytes total.")
    conn.close()
    srv.close()
