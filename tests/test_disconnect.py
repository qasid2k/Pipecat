"""Tests for AsteriskCallSession.disconnect() -- ending a call from OUR side.

The bug these exist for: a live drain test left `PJSIP/101` Up in
`Stasis(voiceagent)` after the bot had exited, still holding the `agents` group.
The caller was abandoned to silence and a capacity slot was gone until Asterisk
restarted. `hangup()` is audio-only on purpose (it must not kill a transferred
call), so nothing ever ended the caller's channel when the BOT decided the call
was over -- on shutdown, on idle timeout, or after an engine crash.

The rule under test: if we ended the call and the caller is still there and we
did not transfer them, hang up on them. Otherwise do not touch their channel.

    python -m unittest discover -s tests -t . -v
"""

import asyncio
import unittest
from unittest import mock

from ari_controller import AriCall
from transports.asterisk import AsteriskCallSession


class FakeIO:
    """Stands in for AudioSocketConnection."""

    def __init__(self, call_id="uuid-1"):
        self.call_id = call_id
        self.hangup_event = asyncio.Event()
        self.stopped = 0
        self.frames_in = self.frames_out = self.frames_out_real = 0

    def stop(self):
        self.stopped += 1


def make_session(with_ari=True):
    io = FakeIO()
    controller = mock.AsyncMock() if with_ari else None
    ari_call = (
        AriCall("PJSIP/101-0000001", "bridge-1", "em-1", "uuid-1", "101")
        if with_ari
        else None
    )
    session = AsteriskCallSession(
        io=io, addr=("127.0.0.1", 5000), controller=controller, ari_call=ari_call
    )
    return session, io, controller


class DisconnectTest(unittest.IsolatedAsyncioTestCase):
    async def test_hangs_up_the_caller_when_we_end_the_call(self):
        """The orphan fix: the bot ended it, so the caller must be told."""
        session, io, controller = make_session()

        await session.disconnect()

        controller.hangup.assert_awaited_once_with("PJSIP/101-0000001")
        self.assertEqual(io.stopped, 1, "the audio path was not closed")

    async def test_does_not_hang_up_a_transferred_call(self):
        """The call belongs to the dialplan now, possibly mid-conversation with
        a human. Hanging it up would cut off the transfer we just made."""
        session, io, controller = make_session()
        await session.transfer("billing")
        controller.reset_mock()

        await session.disconnect()

        controller.hangup.assert_not_awaited()
        self.assertEqual(io.stopped, 1, "the audio path should still close")

    async def test_transferred_flag_is_set_even_if_the_transfer_call_fails(self):
        """Marked before the ARI call, not after: if `continue` lands and then we
        are cancelled, the channel is already gone. Better to wrongly leave a
        channel alone than to hang up a caller being connected to a human."""
        session, io, controller = make_session()
        controller.transfer.side_effect = RuntimeError("ARI died mid-transfer")

        with self.assertRaises(RuntimeError):
            await session.transfer("sales")

        controller.reset_mock()
        await session.disconnect()
        controller.hangup.assert_not_awaited()

    async def test_does_not_hang_up_when_the_caller_already_left(self):
        """They hung up first -- their channel is gone and a DELETE would 404."""
        session, io, controller = make_session()
        io.hangup_event.set()

        await session.disconnect()

        controller.hangup.assert_not_awaited()
        self.assertEqual(io.stopped, 1)

    async def test_direct_call_without_ari_just_closes_audio(self):
        """Extension 6000 has no channel to act on."""
        session, io, _ = make_session(with_ari=False)

        await session.disconnect()

        self.assertEqual(io.stopped, 1)

    async def test_never_raises_when_ari_fails(self):
        """disconnect() runs in a `finally`, often during shutdown. A vendor
        error must not stop the audio path closing or replace a real exception."""
        session, io, controller = make_session()
        controller.hangup.side_effect = RuntimeError("ARI unreachable")

        await session.disconnect()  # must not raise

        self.assertEqual(io.stopped, 1, "audio path left open after an ARI error")

    async def test_is_safe_to_call_twice(self):
        session, io, controller = make_session()

        await session.disconnect()
        await session.disconnect()

        self.assertEqual(io.stopped, 2, "hangup() must tolerate repeats")

    async def test_plain_hangup_still_never_touches_the_channel(self):
        """The original guarantee is unchanged: hangup() is audio-only, which is
        what makes it safe in the post-transfer path."""
        session, io, controller = make_session()

        await session.hangup()

        controller.hangup.assert_not_awaited()
        self.assertEqual(io.stopped, 1)


if __name__ == "__main__":
    unittest.main()
