"""Live check for IMP-002: where does the silence before the greeting go?

Run this ON THE VM, with bot.py running the IMP-002 code. It reads the
`setup:` line every call now logs, shows the steps side by side, names the
slowest one, and checks that /metrics carries the new greeting metric.

    # you make the calls yourself (3 calls to 6001, hear the greeting, hang up):
    python tools/check_greeting_timing.py --since-minutes 10

    # or let it place them through Asterisk (needs `asterisk -rx` access, and
    # the dialplan context that holds extension 6001):
    python tools/check_greeting_timing.py --originate 3 --context <your-context>

WHAT THE STEPS MEAN
-------------------
    connected → correlated          matching the audio socket to its ARI call
    correlated → engine_start       picking an agent, building the engine
    engine_start → vad_built        loading this call's voice detector
    vad_built → pipeline_built      assembling STT / LLM / TTS
    pipeline_built → pipeline_started   opening the Deepgram + Gemini connections
    pipeline_started → first_speech     synthesising the greeting, first audio out

A direct call to 6000 has no ARI step, so it shows `connected → engine_start`.

Exit code 0 = PASS, 1 = FAIL, so it can go in a script. It only READS the log
and /metrics; the one thing it can do is place calls, and only when you ask.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

STEP = re.compile(r"(\w+→\w+) (\d+) ms")
TOTAL = re.compile(r"first speech after (\d+) ms")
METRIC_COUNT = "voiceagent_time_to_greeting_seconds_count"
# The transport's marks. Without them the line is only the engine's half, which
# means setup_marks() is not reaching the engine -- the part IMP-002 added.
TRANSPORT_STEP_PREFIX = "connected→"


def parse_setup(message: str) -> tuple[dict[str, int], int | None]:
    """`setup: a→b 12 ms | … | first speech after 3400 ms` -> (steps, total_ms)."""
    steps = {name: int(ms) for name, ms in STEP.findall(message)}
    total = TOTAL.search(message)
    return steps, int(total.group(1)) if total else None


def read_setup_lines(lines, since_ts: float = 0.0) -> list[dict]:
    """Every `setup:` line in a loguru JSON log (logs/agent.jsonl), oldest first."""
    calls = []
    for raw in lines:
        try:
            record = json.loads(raw)["record"]
        except (ValueError, KeyError, TypeError):
            continue  # not a JSON log line; the console format is not parsed
        message = record.get("message", "")
        if not message.startswith("setup:"):
            continue
        ts = record.get("time", {}).get("timestamp", 0.0)
        if ts < since_ts:
            continue
        steps, total_ms = parse_setup(message)
        calls.append({
            "call_id": str(record.get("extra", {}).get("call_id", "?")),
            "steps": steps,
            "total_ms": total_ms,
        })
    return calls


def summarize(calls: list[dict]) -> dict:
    greeted = [c for c in calls if c["total_ms"] is not None]
    per_step: dict[str, list[int]] = {}
    for c in greeted:
        for name, ms in c["steps"].items():
            per_step.setdefault(name, []).append(ms)
    averages = {name: round(sum(v) / len(v)) for name, v in per_step.items()}
    return {
        "greeted": len(greeted),
        "averages": averages,
        "slowest_step": max(averages, key=averages.get) if averages else None,
        "avg_total_ms": round(sum(c["total_ms"] for c in greeted) / len(greeted)) if greeted else None,
    }


def verdict(calls: list[dict], expected: int, metric_count: int | None) -> tuple[bool, list[str]]:
    """The PASS rules from the IMP-002 backlog item."""
    problems = []
    greeted = [c for c in calls if c["total_ms"] is not None]
    if len(greeted) < expected:
        problems.append(
            f"only {len(greeted)} greeted call(s) have a setup: line; expected {expected}. "
            "Make more calls, widen --since-minutes, or check --log."
        )
    missing = [c["call_id"][:8] for c in greeted
               if not any(s.startswith(TRANSPORT_STEP_PREFIX) for s in c["steps"])]
    if missing:
        problems.append(f"no transport step (connected→…) on call(s) {missing}")
    if metric_count is None:
        problems.append(
            f"/metrics has no {METRIC_COUNT}. Is bot.py running the IMP-002 code? "
            "(`git log --oneline -1`, then restart bot.py)"
        )
    elif metric_count < len(greeted):
        problems.append(f"/metrics counts {metric_count} greetings but the log has {len(greeted)}")
    return not problems, problems


def metric_value(text: str, name: str) -> float | None:
    for line in text.splitlines():
        if line.startswith(name + " "):
            value = float(line.split()[1])
            return int(value) if value.is_integer() else value
    return None


def fetch_metric(url: str) -> int | None:
    try:
        text = urllib.request.urlopen(url, timeout=3).read().decode()
    except OSError as e:
        print(f"  could not read {url}: {e}")
        return None
    return metric_value(text, METRIC_COUNT)


def originate(n: int, context: str, exten: str, hold_s: int) -> None:
    """Place n calls, one at a time, each staying silent for hold_s seconds.

    hold_s defaults to 12 so the call ends BEFORE the silent-caller check-in
    (IMP-001) fires -- that is a different feature and would muddy the log.
    """
    for i in range(1, n + 1):
        cmd = f"channel originate Local/{exten}@{context} application Wait {hold_s}"
        print(f"  call {i}/{n}: asterisk -rx \"{cmd}\"")
        subprocess.run(["asterisk", "-rx", cmd], check=False)
        time.sleep(hold_s + 3)  # let it finish and the log line land


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log", default="logs/agent.jsonl")
    ap.add_argument("--since-minutes", type=float, default=15)
    ap.add_argument("--expect", type=int, default=3, help="greeted calls needed to PASS")
    ap.add_argument("--metrics", default="http://127.0.0.1:8091/metrics")
    ap.add_argument("--originate", type=int, default=0, metavar="N",
                    help="place N test calls through Asterisk first")
    ap.add_argument("--context", help="dialplan context holding the extension (with --originate)")
    ap.add_argument("--exten", default="6001")
    ap.add_argument("--hold", type=int, default=12, help="seconds each placed call stays up")
    args = ap.parse_args()

    started = time.time()
    if args.originate:
        if not args.context:
            ap.error("--originate needs --context (the context that holds extension 6001)")
        print(f"Placing {args.originate} call(s) to {args.exten}@{args.context} …")
        originate(args.originate, args.context, args.exten, args.hold)
        since = started - 5
    else:
        since = started - args.since_minutes * 60

    log = Path(args.log)
    if not log.exists():
        print(f"FAIL: no log at {log}. Run from the repo root, or pass --log.")
        return 1
    calls = read_setup_lines(log.read_text(encoding="utf-8", errors="replace").splitlines(), since)

    print(f"\n{len(calls)} call(s) with a setup: line in {log}:")
    for c in calls:
        total = f"{c['total_ms']} ms" if c["total_ms"] is not None else "no greeting"
        print(f"  {c['call_id'][:8]}  first speech: {total}")

    s = summarize(calls)
    if s["averages"]:
        print(f"\nAverage over {s['greeted']} greeted call(s):")
        width = max(len(k) for k in s["averages"])
        for name, ms in s["averages"].items():
            mark = "   <-- slowest" if name == s["slowest_step"] else ""
            print(f"  {name:<{width}}  {ms:>6} ms{mark}")
        print(f"  {'TOTAL (connect to first speech)':<{width}}  {s['avg_total_ms']:>6} ms")

    metric_count = fetch_metric(args.metrics)
    print(f"\n/metrics {METRIC_COUNT} = {metric_count}")

    ok, problems = verdict(calls, args.expect, metric_count)
    print("\nPASS" if ok else "\nFAIL")
    for p in problems:
        print(f"  - {p}")
    if ok and s["slowest_step"]:
        print(f"\nThe wait is mostly `{s['slowest_step']}`. Tell Claude these numbers.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
