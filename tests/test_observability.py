"""Stage B: can one call be reconstructed afterwards?

Everything later -- the database, the dashboard, QA scoring -- reads what these
tests protect. The failure they exist to prevent is the quiet one: records that
look fine individually and cannot be joined to anything, which is exactly what
the audit found before Stage B (recordings whose filenames and contents shared
no identifier with the call that produced them).

    python -m unittest discover -s tests -t . -v
"""

import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from loguru import logger

from ari_controller import AriCall
from core.logging import NO_CALL, configure_logging
from engine.transcripts import TranscriptRecorder, save_conversation
from transports.asterisk import AsteriskCallSession


class FakeIO:
    def __init__(self, call_id="uuid-1"):
        self.call_id = call_id
        self.hangup_event = asyncio.Event()
        self.frames_in = 10
        self.frames_out = 20
        self.frames_out_real = 5
        self.frames_dropped = 0
        self.pacer_slips = 0

    def stop(self):
        pass


class VendorIdsTest(unittest.TestCase):
    """The join keys. These cannot be backfilled once the channel is gone."""

    def test_asterisk_session_exposes_the_ids_that_join_to_cdr(self):
        # channel_id is what ARI calls channel.id, and it IS Asterisk's
        # uniqueid -- hence the "1787584901.399" shape rather than a PJSIP name.
        ari_call = AriCall(
            "1787584901.399", "bridge-1", "em-1", "uuid-1", "101",
            linkedid="1787584901.396",
        )
        session = AsteriskCallSession(
            io=FakeIO(), addr=("127.0.0.1", 5000), controller=object(),
            ari_call=ari_call,
        )
        ids = session.vendor_ids
        self.assertEqual(ids["uniqueid"], "1787584901.399")
        self.assertEqual(ids["linkedid"], "1787584901.396")
        # One key per value: uniqueid and channel_id are the same string, and
        # exposing both only raises the question of which is authoritative.
        self.assertNotIn("channel_id", ids)

    def test_a_call_without_ari_reports_no_vendor_ids(self):
        """A direct call to 6000 has no channel; empty is correct, not an error."""
        session = AsteriskCallSession(io=FakeIO(), addr=("127.0.0.1", 5000))
        self.assertEqual(session.vendor_ids, {})


class OverloadCountersTest(unittest.TestCase):
    """`frames_dropped` and `pacer_slips` were incremented and never read."""

    def make(self, dropped=0, slips=0):
        io = FakeIO()
        io.frames_dropped = dropped
        io.pacer_slips = slips
        return AsteriskCallSession(io=io, addr=("127.0.0.1", 5000))

    def test_a_healthy_call_stays_quiet(self):
        self.assertNotIn("DROPPED", self.make().stats())

    def test_dropped_frames_are_reported(self):
        stats = self.make(dropped=7).stats()
        self.assertIn("DROPPED=7", stats)

    def test_pacer_slips_are_reported(self):
        self.assertIn("slips=3", self.make(slips=3).stats())


class TranscriptTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_file_says_which_call_it_belongs_to(self):
        """The orphaned-recording fix: a transcript must be self-describing.

        Before Stage B a transcript on disk could not be matched to its call,
        caller, agent or Asterisk channel except by matching wall-clock seconds
        against the log.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            meta = {
                "call_id": "uuid-1",
                "caller_id": "101",
                "tenant_id": "techbridge",
                "persona": "Sarah",
                "uniqueid": "1787584901.399",
            }
            rec = TranscriptRecorder(path, call=meta)
            rec._append({"type": "turn", "seq": 1, "speaker": "caller", "text": "hi"})
            await rec.close()

            lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(lines[0]["type"], "call")
        for key, value in meta.items():
            self.assertEqual(lines[0][key], value, f"{key} missing from the header")
        self.assertEqual(lines[1]["text"], "hi")

    async def test_writing_does_not_block_the_event_loop(self):
        """Writes used to be synchronous file I/O in the frame handler, on the
        loop -- the same hazard that dropped calls in B-001 and B-011."""
        with tempfile.TemporaryDirectory() as tmp:
            rec = TranscriptRecorder(Path(tmp) / "t.jsonl", call={"call_id": "x"})
            loop = asyncio.get_running_loop()
            started = loop.time()
            for i in range(200):
                rec._append({"type": "turn", "seq": i, "text": "x" * 200})
            # _append only enqueues; it must return essentially instantly even
            # though 200 records still have to reach the disk.
            self.assertLess(loop.time() - started, 0.5, "_append is doing I/O inline")
            await rec.close()
            self.assertEqual(
                len((Path(tmp) / "t.jsonl").read_text(encoding="utf-8").splitlines()),
                201,  # 200 turns + the header
                "close() lost records that were still queued",
            )

    async def test_close_flushes_the_last_thing_the_caller_said(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            rec = TranscriptRecorder(path, call={"call_id": "x"})
            rec._append({"type": "turn", "text": "the last thing I said"})
            await rec.close()
            self.assertIn("the last thing I said", path.read_text(encoding="utf-8"))

    async def test_close_is_safe_when_nothing_was_ever_written(self):
        """A call where nobody spoke must not leave a stuck task."""
        with tempfile.TemporaryDirectory() as tmp:
            rec = TranscriptRecorder(Path(tmp) / "t.jsonl", call={"call_id": "x"})
            await rec.close()
            self.assertFalse((Path(tmp) / "t.jsonl").exists())


class ConversationFileTest(unittest.TestCase):
    class FakeContext:
        messages = [
            {"role": "system", "content": "you are Sarah"},
            {"role": "assistant", "content": "Hi, this is Sarah."},
            {"role": "user", "content": "I need billing."},
        ]

    def test_conversation_carries_the_identifiers_and_a_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            meta = {"call_id": "uuid-1", "persona": "Sarah", "uniqueid": "178.399",
                    "end_reason": "caller hung up"}
            save_conversation(self.FakeContext(), path, datetime.now(), call=meta)
            data = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(data["call"]["call_id"], "uuid-1")
        self.assertEqual(data["call"]["uniqueid"], "178.399")
        self.assertEqual(data["call"]["end_reason"], "caller hung up")
        self.assertIn("duration_s", data)
        # The system prompt must never be written to disk: it is not part of the
        # conversation, and it is the one message that is the same for everyone.
        self.assertEqual(data["turns"], 2)
        self.assertNotIn("system", [m["role"] for m in data["conversation"]])


class LoggingTest(unittest.TestCase):
    def tearDown(self):
        logger.remove()

    def test_json_sink_records_call_id_as_a_field(self):
        """Not text inside a message -- a field, so three concurrent calls can
        be separated by a query rather than a regex."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            configure_logging(file=path, tenant_id="techbridge")
            with logger.contextualize(call_id="uuid-1"):
                logger.info("assigned 'Sarah'")
            logger.info("startup line, no call")
            logger.remove()  # flush the enqueued sink

            lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]

        by_msg = {json.loads(json.dumps(r))["record"]["message"]: r for r in lines}
        call_line = by_msg["assigned 'Sarah'"]["record"]["extra"]
        self.assertEqual(call_line["call_id"], "uuid-1")
        self.assertEqual(call_line["tenant_id"], "techbridge")
        self.assertEqual(
            by_msg["startup line, no call"]["record"]["extra"]["call_id"], NO_CALL
        )

    def test_bindings_do_not_leak_between_concurrent_calls(self):
        """contextualize() is a contextvar, so two tasks must not see each
        other's call_id. If they did, every record downstream would be wrong."""

        async def scenario(path):
            seen = {}

            async def call(name):
                with logger.contextualize(call_id=name):
                    await asyncio.sleep(0)  # force interleaving
                    logger.info(f"line from {name}")
                    seen[name] = True

            await asyncio.gather(call("call-a"), call("call-b"))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.jsonl"
            configure_logging(file=path)
            asyncio.run(scenario(path))
            logger.remove()
            records = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]

        for r in records:
            msg = r["record"]["message"]
            if msg.startswith("line from "):
                self.assertEqual(
                    r["record"]["extra"]["call_id"], msg.rsplit(" ", 1)[1],
                    "a call's log line carried another call's id",
                )


if __name__ == "__main__":
    unittest.main()
