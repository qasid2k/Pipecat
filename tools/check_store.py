"""Prove the call-record store works, without needing a phone call.

Stage C part 1 is deliberately not wired into the call path, so no phone call
exercises it. This does: it uses the REAL config, the REAL factory, the REAL
writer and the REAL store, writes one call plus two turns exactly the way
`run_call` will, reads them back, and deletes them again.

    python tools/check_store.py

It is also the smoke test to re-run after swapping the backend (Postgres, when
there is an instance) -- the whole point of putting the store behind an
interface is that this script should not have to change.
"""

import asyncio
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ConfigError, load_config  # noqa: E402
from core.records import CallRecord, RecordWriter, TurnRecord, utcnow  # noqa: E402
from factories import create_call_store  # noqa: E402

MARKER = "TEST-store-check"


async def main() -> int:
    try:
        config = load_config()
    except ConfigError as e:
        print(f"config problem:\n{e}")
        return 1

    store = create_call_store(config)
    print(f"store      : {store.describe}")
    if store.describe == "disabled":
        print("records are switched off (service.records.enabled: false) -- nothing to check")
        return 0

    writer = RecordWriter(store)
    await writer.start()

    started = utcnow()
    writer.submit(
        CallRecord(
            call_id=MARKER,
            tenant_id=config.service.tenant_id,
            started_at=started,
            ended_at=started + timedelta(seconds=42.5),
            duration_s=42.5,
            caller_id="101",
            uniqueid="9999999999.999",
            linkedid="9999999999.999",
            persona="Sarah",
            voice="aura-2-thalia-en",
            llm_model=config.engine.llm.model,
            end_reason="caller hung up",
            cause="call ended -- caller hung up",
            transferred_to="billing",
            frames_in=2100,
            frames_out=2125,
            frames_out_real=430,
        )
    )
    writer.submit(
        [
            TurnRecord(MARKER, 1, utcnow(), "caller", "I need billing"),
            TurnRecord(MARKER, 2, utcnow(), "agent", "Putting you through."),
        ]
    )
    await writer.close()
    print(f"writer     : {writer.stats}")

    # Read it back the way a dashboard or an analyst would -- deliberately with a
    # plain query rather than through the store, so this checks what actually
    # landed on disk rather than what we think we wrote.
    conn = sqlite3.connect(store._path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM calls WHERE call_id = ?", (MARKER,)
        ).fetchone()
        turns = conn.execute(
            "SELECT seq, speaker, text FROM turns WHERE call_id = ? ORDER BY seq",
            (MARKER,),
        ).fetchall()

        if row is None:
            print("\nFAIL: nothing was written")
            return 1

        print("\ncall row:")
        for key in (
            "call_id", "tenant_id", "persona", "voice", "duration_s",
            "uniqueid", "linkedid", "transferred_to", "node_id",
        ):
            print(f"  {key:15} {row[key]}")

        print("\nturns:")
        for t in turns:
            print(f"  {t['seq']}. {t['speaker']:6} {t['text']}")

        print("\nthe questions this table exists to answer:")
        queries = {
            "calls per agent": (
                "SELECT persona, COUNT(*) AS calls, "
                "ROUND(AVG(duration_s), 1) AS avg_s FROM calls GROUP BY persona"
            ),
            "transfer rate": (
                "SELECT COUNT(*) AS total, "
                "SUM(transferred_to IS NOT NULL) AS transferred FROM calls"
            ),
            "overloaded calls": (
                "SELECT COUNT(*) AS n FROM calls "
                "WHERE frames_dropped > 0 OR pacer_slips > 0"
            ),
        }
        for label, sql in queries.items():
            print(f"  {label:18} {[dict(r) for r in conn.execute(sql)]}")

        conn.execute("DELETE FROM calls WHERE call_id = ?", (MARKER,))
        conn.execute("DELETE FROM turns WHERE call_id = ?", (MARKER,))
        conn.commit()
        print(f"\ntest rows removed. PASS -- the store is real and queryable.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
