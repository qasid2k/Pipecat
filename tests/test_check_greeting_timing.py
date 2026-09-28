"""The IMP-002 live-check script's parsing and verdict.

`tools/check_greeting_timing.py` runs on the VM against real calls; this only
checks that it reads the `setup:` lines the engine actually writes, and that
its PASS/FAIL rules are the ones promised in the backlog.
"""

import json
import unittest

from engine.timing import format_setup
from tools.check_greeting_timing import (
    metric_value,
    parse_setup,
    read_setup_lines,
    summarize,
    verdict,
)

GREETED = format_setup(
    [("connected→correlated", 40), ("correlated→engine_start", 5),
     ("pipeline_built→pipeline_started", 900), ("pipeline_started→first_speech", 2100)],
    3.045,
)
UNGREETED = format_setup([("connected→correlated", 30)], None)


class ParseTest(unittest.TestCase):
    def test_reads_what_the_engine_writes(self):
        steps, total_ms = parse_setup(GREETED)
        self.assertEqual(steps["pipeline_started→first_speech"], 2100)
        self.assertEqual(len(steps), 4)
        self.assertEqual(total_ms, 3045)

    def test_a_call_with_no_greeting_has_no_total(self):
        steps, total_ms = parse_setup(UNGREETED)
        self.assertEqual(steps, {"connected→correlated": 30})
        self.assertIsNone(total_ms)

    def test_reads_the_json_log_and_skips_everything_else(self):
        def line(msg, call="c1", ts=1000.0):
            return json.dumps({"text": msg, "record": {
                "message": msg, "extra": {"call_id": call},
                "time": {"timestamp": ts}}})

        log = "\n".join([
            line("assigned 'Alex'"),
            line(GREETED, "c1", 1000.0),
            "not json at all",
            line(UNGREETED, "c2", 2000.0),
            line(GREETED, "old", 10.0),
        ])
        calls = read_setup_lines(log.splitlines(), since_ts=500.0)
        self.assertEqual([c["call_id"] for c in calls], ["c1", "c2"])


class SummaryAndVerdictTest(unittest.TestCase):
    def calls(self):
        return [
            {"call_id": "a", "steps": parse_setup(GREETED)[0], "total_ms": 3045},
            {"call_id": "b", "steps": parse_setup(GREETED)[0], "total_ms": 3045},
            {"call_id": "c", "steps": parse_setup(GREETED)[0], "total_ms": 3045},
        ]

    def test_the_slowest_step_is_named(self):
        summary = summarize(self.calls())
        self.assertEqual(summary["slowest_step"], "pipeline_started→first_speech")
        self.assertEqual(summary["avg_total_ms"], 3045)

    def test_passes_with_enough_greeted_calls_and_the_metric(self):
        ok, problems = verdict(self.calls(), expected=3, metric_count=3)
        self.assertTrue(ok, problems)

    def test_fails_without_enough_calls(self):
        ok, problems = verdict(self.calls()[:1], expected=3, metric_count=1)
        self.assertFalse(ok)
        self.assertTrue(any("3" in p for p in problems))

    def test_fails_when_the_metric_is_missing(self):
        # A missing metric means the VM is running code from before IMP-002.
        ok, problems = verdict(self.calls(), expected=3, metric_count=None)
        self.assertFalse(ok)

    def test_fails_when_the_transport_marks_are_missing(self):
        calls = self.calls()
        calls[0]["steps"] = {"engine_start→vad_built": 170}
        ok, _ = verdict(calls, expected=3, metric_count=3)
        self.assertFalse(ok)

    def test_metric_parsing(self):
        text = (
            "# TYPE voiceagent_time_to_greeting_seconds summary\n"
            "voiceagent_time_to_greeting_seconds_sum 9.135\n"
            "voiceagent_time_to_greeting_seconds_count 3\n"
        )
        self.assertEqual(metric_value(text, "voiceagent_time_to_greeting_seconds_count"), 3)
        self.assertIsNone(metric_value(text, "voiceagent_nope"))


if __name__ == "__main__":
    unittest.main()
