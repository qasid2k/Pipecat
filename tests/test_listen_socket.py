"""The listening socket's two platform-specific options.

Both exist because of a real outage, and both are easy to break by tidying:

  * `SO_RCVBUF` must be set BEFORE bind, or Asterisk drops calls with "Resource
    temporarily unavailable" ([[bugs]] B-001). TCP fixes its window scale during
    the handshake, from the *listening* socket, so enlarging the buffer after
    accept() is too late.
  * `SO_REUSEADDR` must be set on POSIX and never on Windows ([[bugs]] B-003).
    Off everywhere, restarting on Linux fails with EADDRINUSE while the previous
    run's sockets sit in TIME_WAIT. On everywhere, Windows refuses to bind at all.

    python -m unittest discover -s tests -t . -v
"""

import socket
import sys
import unittest
from unittest import mock

from transports import asterisk
from transports.asterisk import AsteriskTransport

PORT = 18098


def transport(port=PORT):
    return AsteriskTransport(host="127.0.0.1", port=port, ari_password=None)


class ListenSocketTest(unittest.TestCase):
    def test_reuseaddr_is_set_on_posix(self):
        """Without it, a restart is a coin flip against TIME_WAIT."""
        with mock.patch.object(asterisk.sys, "platform", "linux"):
            sock = transport(PORT + 1)._make_listen_socket()
        try:
            self.assertTrue(
                sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR),
                "SO_REUSEADDR must be set on POSIX",
            )
        finally:
            sock.close()

    def test_reuseaddr_is_never_set_on_windows(self):
        """It means something different and hostile there -- B-003."""
        with mock.patch.object(asterisk.sys, "platform", "win32"):
            sock = transport(PORT + 2)._make_listen_socket()
        try:
            self.assertFalse(
                sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR),
                "SO_REUSEADDR on Windows raises WinError 10013 on bind",
            )
        finally:
            sock.close()

    def test_receive_buffer_is_enlarged(self):
        """B-001. The kernel may round or double the request, so check it grew
        rather than checking an exact number."""
        sock = transport(PORT + 3)._make_listen_socket()
        try:
            self.assertGreaterEqual(
                sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
                asterisk.RECV_BUFFER_BYTES // 2,
            )
        finally:
            sock.close()

    def test_a_second_bind_still_fails(self):
        """SO_REUSEADDR is not SO_REUSEPORT. Two live bots sharing the port would
        be far worse than a failed start, and 'another bot is running' is the
        likeliest cause of EADDRINUSE -- it must stay loud."""
        first = transport(PORT + 4)._make_listen_socket()
        first.listen(5)
        try:
            with self.assertRaises(OSError):
                transport(PORT + 4)._make_listen_socket()
        finally:
            first.close()

    def test_the_bind_error_explains_both_causes(self):
        """The raw errno says nothing about which cause it is, and the two need
        opposite responses -- stop the other process, or just wait."""
        first = transport(PORT + 5)._make_listen_socket()
        first.listen(5)
        try:
            with self.assertRaises(OSError) as caught:
                transport(PORT + 5)._make_listen_socket()
            message = str(caught.exception)
            # The drain is listed FIRST because it is the cause we created
            # ourselves and the one that catches people out: restarting within
            # drain_timeout_s of Ctrl+C lands here while the old run is still
            # letting its calls finish.
            self.assertIn("SHUTTING DOWN", message)
            self.assertIn("drain", message)
            self.assertIn("pgrep -af bot.py", message)
            self.assertIn(str(PORT + 5), message)
        finally:
            first.close()


if __name__ == "__main__":
    unittest.main()
